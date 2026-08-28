"""Deterministic Seed executability preflight.

The seed_2be2907edc07 post-mortem: a Seed reached RUN with verify scripts
that never existed (claimed in ``existing_dependencies``), an unbound
``$VAULT_PATH`` in every verify command, and brownfield context references
that were concepts ("Obsidian Vault"), not paths. The LLM QA judge passed
it. These tests pin the deterministic gate that catches each fabrication
class and phrases every finding as an open question, never a rewrite.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

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
    """Nested-shell assignments bind later expansions in that same shell."""
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Pipeline runs in an isolated temp dir",
                verify_command=(
                    'sh -c \'tmp=$(mktemp -d) && cp organize.py "$tmp"/ '
                    '&& cd "$tmp" && python organize.py && echo OK\''
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
        ("/definitely/missing/verify.sh", "/definitely/missing/verify.sh"),
        ("../scripts/verify.py", "../scripts/verify.py"),
        ('/bin/bash -c "/definitely/missing/verify.sh"', "/definitely/missing/verify.sh"),
        ("python3.14t check.py", "check.py"),
        ("pypy3 check.py", "check.py"),
        ("python --check-hash-based-pycs always check.py", "check.py"),
        ('sh -c "python check.py"', "check.py"),
        ("bash -c ./verify", "verify"),
        ("node --require preload.js missing.js", "missing.js"),
        ("ruby -I lib missing.rb", "missing.rb"),
        ("bash -O extglob ./missing-verify", "missing-verify"),
        ("FOO=bar echo ok; ./missing-verify", "missing-verify"),
        ("env FOO=bar ./missing-verify", "missing-verify"),
        ("timeout 10 ./missing-verify", "missing-verify"),
        ("command ./missing-verify", "missing-verify"),
        ("nice ./missing-verify", "missing-verify"),
        ("env -S 'python missing.py'", "missing.py"),
        ('env --split-string="python missing.py"', "missing.py"),
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


@pytest.mark.parametrize(
    ("command", "expected_code", "expected_subject"),
    (
        ("bash -eu -c 'test -n \"$MISSING\"'", "unbound_env_var", "$MISSING"),
        ("bash -eu -c 'python missing.py'", "verify_program_missing", "missing.py"),
        ("sh -eu -c 'test -n \"$MISSING\"'", "unbound_env_var", "$MISSING"),
        ("sh -eu -c 'python missing.py'", "verify_program_missing", "missing.py"),
        ("bash -euc 'test -n \"$MISSING\"'", "unbound_env_var", "$MISSING"),
        ("sh -euc 'python missing.py'", "verify_program_missing", "missing.py"),
        (
            "bash --rcfile /dev/null -c 'set -u; test -n \"$MISSING\"'",
            "unbound_env_var",
            "$MISSING",
        ),
        (
            "bash --rcfile /dev/null -c 'python missing.py'",
            "verify_program_missing",
            "missing.py",
        ),
        (
            "bash --init-file /dev/null -c 'set -u; test -n \"$MISSING\"'",
            "unbound_env_var",
            "$MISSING",
        ),
        (
            "bash --init-file /dev/null -c 'python missing.py'",
            "verify_program_missing",
            "missing.py",
        ),
    ),
)
def test_option_bearing_nested_shell_matches_bin_sh_failure(
    tmp_path: Path,
    monkeypatch,
    command: str,
    expected_code: str,
    expected_subject: str,
) -> None:
    monkeypatch.delenv("MISSING", raising=False)
    environment = os.environ.copy()

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Nested verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [(finding.code, finding.subject) for finding in report.blocking_findings] == [
        (expected_code, expected_subject)
    ]
    assert (
        subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env=environment,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    "command",
    (
        "node -e missing.js",
        "node --eval missing.js",
        "node -p missing.js",
        "ruby -e check.rb",
    ),
)
def test_inline_runner_source_is_not_classified_as_a_missing_program(
    tmp_path: Path, command: str
) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(description="Inline verifier passes", verify_command=command),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert not report.blocking_findings


@pytest.mark.parametrize(
    "command",
    (
        "bash -c 'source missing.sh'",
        "perl missing.pl",
        "php missing.php",
        "lua missing.lua",
    ),
)
def test_missing_source_and_interpreter_programs_block_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Interpreter verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(
    ("command", "program", "content"),
    (
        ("bash -c 'source check.sh'", "check.sh", "exit 0\n"),
        ("perl check.pl", "check.pl", "exit 0;\n"),
    ),
)
def test_existing_source_and_interpreter_programs_pass_with_shell_parity(
    tmp_path: Path, command: str, program: str, content: str
) -> None:
    (tmp_path / program).write_text(content, encoding="utf-8")
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Interpreter verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    ("setup", "command"),
    (
        ("FOO=bar\n", '. ./setup.sh; set -u; test "$FOO" = bar'),
        (
            "mkdir -p sub\nprintf 'raise SystemExit(0)\\n' > sub/check.py\ncd sub\n",
            ". ./setup.sh; python check.py",
        ),
    ),
)
def test_sourced_state_is_inconclusive_without_false_blockers(
    tmp_path: Path, monkeypatch, setup: str, command: str
) -> None:
    monkeypatch.delenv("FOO", raising=False)
    (tmp_path / "setup.sh").write_text(setup, encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Sourced verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert not report.blocking_findings
    assert [finding.code for finding in report.findings if not finding.blocking] == [
        "verify_shell_state_inconclusive"
    ]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_sourced_state_that_invalidates_host_bindings_remains_inconclusive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FOO", "bar")
    (tmp_path / "setup.sh").write_text("unset FOO\n", encoding="utf-8")
    command = '. ./setup.sh; test "$FOO" = bar'

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Sourced verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert not report.blocking_findings
    assert "verify_shell_state_inconclusive" in {finding.code for finding in report.findings}
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(
    "command",
    (
        "uv run python missing.py",
        "uv run --python 3.12 python missing.py",
        "uv run -- python missing.py",
    ),
)
def test_uv_run_missing_verifier_blocks_with_shell_parity(tmp_path: Path, command: str) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="UV verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "uv"
    shim.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    shim.chmod(0o755)
    environment = {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    assert (
        subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env=environment,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    "command",
    (
        "npm exec --offline -- ./missing.sh",
        "npx --offline ./missing.sh",
        "pnpm exec ./missing.sh",
        "yarn exec ./missing.sh",
        "bun run ./missing.ts",
    ),
)
def test_package_runner_missing_verifier_blocks_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Package runner verifier", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    # Do not invoke real package managers in the unit suite: even offline
    # modes may consult user caches or start background daemons. A deterministic
    # failing shim preserves the shell's command-dispatch assertion while the
    # preflight above validates the actual runner grammar and missing operand.
    runner = command.split(maxsplit=1)[0]
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / runner
    shim.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    shim.chmod(0o755)
    environment = {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    assert (
        subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env=environment,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    "command",
    (
        "python -c 'print(\"$LITERAL\")'",
        r"printf '%s' \$LITERAL",
    ),
)
def test_literal_or_escaped_dollar_is_not_an_environment_expansion(
    tmp_path: Path, command: str
) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(description="Literal verifier", verify_command=command),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert all(finding.code != "unbound_env_var" for finding in report.findings)


def test_backquote_assignment_does_not_bind_outer_shell_variable(tmp_path: Path) -> None:
    command = ': `${FOO:=true}`; set -u; test -n "$FOO"'
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Backquote scope verifier", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.subject for finding in report.blocking_findings] == ["$FOO"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


def test_eval_established_variable_is_inconclusive_and_matches_shell_success(
    tmp_path: Path,
) -> None:
    command = "eval 'FOO=bar'; set -u; test -n \"$FOO\""

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Eval variable", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert not report.blocking_findings
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(("program", "returncode"), (("check.py", 0), ("missing.py", 2)))
def test_eval_established_directory_keeps_program_resolution_inconclusive(
    tmp_path: Path, program: str, returncode: int
) -> None:
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()
    (subdirectory / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    command = f"eval 'cd sub'; python {program}"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Eval directory", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert not report.blocking_findings
    assert (
        subprocess.run(
            ["/bin/sh", "-c", command], cwd=tmp_path, capture_output=True, check=False
        ).returncode
        == returncode
    )


@pytest.mark.parametrize(
    ("command", "expected_blocking"),
    (
        ("printf '%s\\n' python missing.py", False),
        ("env --split-string=python3 missing.py", True),
        ("sh -c 'test -n \"$REQUIRED\"'", True),
        ('printf -- REQUIRED=value; test -n "$REQUIRED"', True),
    ),
)
def test_preflight_uses_command_position_and_nested_shell_semantics(
    tmp_path: Path, command: str, expected_blocking: bool
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    if expected_blocking:
        assert any(finding.blocking for finding in report.findings)
    else:
        assert not any(finding.blocking for finding in report.findings)


def test_separate_nested_shells_do_not_share_variable_bindings(tmp_path: Path) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Independent shells",
                    verify_command="sh -c 'FOO=x'; sh -c 'echo $FOO'",
                ),
            )
        ),
        workspace_root=tmp_path,
    )
    assert [finding.subject for finding in report.blocking_findings] == ["$FOO"]


@pytest.mark.parametrize(
    ("command", "variable"),
    (
        ("env sh -c 'echo $MISSING'", "$MISSING"),
        ("timeout 1 sh -c 'echo $MISSING'", "$MISSING"),
        ("nice command -- env bash --command 'echo $MISSING'", "$MISSING"),
        ("echo $OUTER; sh -c 'echo ok'", "$OUTER"),
        ("env -S \"timeout 1 sh -c 'echo $MISSING'\"", "$MISSING"),
    ),
)
def test_wrapped_nested_and_outer_shell_scopes_are_all_scanned(
    tmp_path: Path, command: str, variable: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Independent scopes", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert variable in {finding.subject for finding in report.blocking_findings}


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ('env TOKEN=$MISSING sh -c "echo ok"', {"$MISSING"}),
        ('FOO=bar printf "%s\\n" "$FOO"', {"$FOO"}),
        ('export FOO=bar; printf "%s\\n" "$FOO"', set()),
        ('FOO=bar export FOO; printf "%s\\n" "$FOO"', set()),
        ('FOO=bar readonly FOO; printf "%s\\n" "$FOO"', set()),
        ('FOO=bar; printf "%s\\n" "$FOO"', set()),
    ),
)
def test_shell_expansion_precedes_prefix_assignment_and_builtins_persist(
    tmp_path: Path, command: str, expected: set[str]
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Shell binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert {finding.subject for finding in report.blocking_findings} == expected


@pytest.mark.parametrize(
    "command",
    (
        "env FOO=bar sh -c 'printf %s \"$FOO\"'",
        "FOO=bar sh -c 'printf %s \"$FOO\"'",
    ),
)
def test_nested_shell_inherits_launcher_environment_binding(tmp_path: Path, command: str) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Nested binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed


def test_nested_shell_inherits_prior_sequential_export(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    command = "export VAULT_PATH=/tmp; sh -c 'python check.py --vault \"$VAULT_PATH\"'"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Exported binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed


@pytest.mark.parametrize(
    ("command", "runtime_passed"),
    (
        ("FOO=bar\nsh -uc 'test \"$FOO\" = bar'", False),
        ("export FOO=bar\nsh -uc 'test \"$FOO\" = bar'", True),
        ("set -a\nFOO=bar\nsh -uc 'test \"$FOO\" = bar'", True),
    ),
)
def test_newline_separated_nested_shell_state_matches_posix_shell(
    tmp_path: Path, command: str, runtime_passed: bool
) -> None:
    environment = os.environ.copy()
    environment.pop("FOO", None)
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Multiline shell state", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert report.passed is runtime_passed
    assert (runtime.returncode == 0) is runtime_passed


@pytest.mark.parametrize(
    ("command", "runtime_passed"),
    (
        ("false && export FOO=bar; sh -uc 'test \"$FOO\" = bar'", False),
        ("true || export FOO=bar; sh -uc 'test \"$FOO\" = bar'", False),
        ("false && FOO=bar; export FOO; sh -uc 'test \"$FOO\" = bar'", False),
        ("true && export FOO=bar; sh -uc 'test \"$FOO\" = bar'", True),
        ("false || export FOO=bar; sh -uc 'test \"$FOO\" = bar'", True),
    ),
)
def test_conditional_nested_shell_exports_match_posix_shell(
    tmp_path: Path, command: str, runtime_passed: bool
) -> None:
    environment = os.environ.copy()
    environment.pop("FOO", None)
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Conditional nested export", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert report.passed is runtime_passed
    assert (runtime.returncode == 0) is runtime_passed


def test_nested_shell_inherits_prefixed_export_with_shell_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOO", raising=False)
    command = "FOO=bar export FOO; sh -uc 'test \"$FOO\" = bar'"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Prefixed export", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_getopts_target_binding_matches_posix_shell(tmp_path: Path) -> None:
    command = 'unset OPT; set -u; getopts x OPT -x; test "$OPT" = x'
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="getopts binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "special_builtin",
    (
        ":",
        "set -- x",
        "shift",
        "times",
        "trap : 0",
        "unset UNUSED",
    ),
)
def test_assignment_prefix_on_posix_special_builtin_persists(
    tmp_path: Path, special_builtin: str
) -> None:
    setup = "set -- x; " if special_builtin == "shift" else ""
    command = f'{setup}unset PR1944_FOO; set -u; PR1944_FOO=bar {special_builtin}; test "$PR1944_FOO" = bar'
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="POSIX special builtin binding",
                    verify_command=command,
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "set_commands",
    (
        "set -a",
        "set -o allexport",
        "set -a; set +a",
        "set -o allexport; set +o allexport",
    ),
)
def test_allexport_assignment_reaches_nested_shell_with_posix_parity(
    tmp_path: Path, set_commands: str
) -> None:
    if "+" in set_commands:
        enable, disable = set_commands.split("; ")
        command = f"unset FOO; {enable}; FOO=bar; {disable}; sh -uc 'test \"$FOO\" = bar'"
    else:
        command = f"unset FOO; {set_commands}; FOO=bar; sh -uc 'test \"$FOO\" = bar'"
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="allexport binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_allexport_special_builtin_prefix_reaches_nested_shell(
    tmp_path: Path,
) -> None:
    command = "set -a; FOO=bar :; sh -uc 'test \"$FOO\" = bar'"
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="allexport special builtin prefix",
                    verify_command=command,
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    ("command", "passed"),
    (
        ("export FOO; sh -c 'set -u; printf %s \"$FOO\"'", False),
        ("FOO=bar; export FOO; sh -c 'set -u; printf %s \"$FOO\"'", True),
        ("export FOO; FOO=bar; sh -c 'set -u; printf %s \"$FOO\"'", True),
    ),
)
def test_nested_shell_requires_concrete_value_for_bare_export(
    tmp_path: Path, command: str, passed: bool
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Bare export", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed is passed
    environment = os.environ.copy()
    environment.pop("FOO", None)
    environment.pop("HOME", None)
    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert (runtime.returncode == 0) is passed
    assert [finding.subject for finding in report.blocking_findings] == ([] if passed else ["$FOO"])


@pytest.mark.parametrize(
    ("command", "passed"),
    (
        ('FOO=bar; sh -c "printf %s $FOO"', True),
        ('FOO=bar; sh -c "printf %s \\$FOO"', False),
    ),
)
def test_nested_shell_preserves_outer_double_quote_expansion_ownership(
    tmp_path: Path, command: str, passed: bool
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Nested expansion", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed is passed
    assert [finding.subject for finding in report.blocking_findings] == ([] if passed else ["$FOO"])


@pytest.mark.parametrize("operator", ("=", ":="))
def test_assignment_default_parameter_expansion_is_bound(tmp_path: Path, operator: str) -> None:
    command = f'printf %s "${{OUTPUT{operator}default}}"'
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Defaulted output", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed


@pytest.mark.parametrize("operator", ("+", ":+"))
def test_optional_parameter_expansion_does_not_require_binding(
    tmp_path: Path, operator: str
) -> None:
    command = f'printf %s "${{OUTPUT{operator}alternate}}"'
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Optional output", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed


@pytest.mark.parametrize(
    ("command", "passed"),
    (
        ('FOO=bar; unset FOO; test -n "$FOO"', False),
        ('printf %s "${FOO:=default}"; test "$FOO" = default', True),
        ('printf %s "${FOO=default}"; test "$FOO" = default', True),
        ("env -u HOME sh -c 'test -n \"$HOME\"'", False),
        ("env -u HOME sh -c 'HOME=/tmp; test -n \"$HOME\"'", True),
        ("env -i sh -c 'test -n \"$HOME\"'", False),
        ("env --ignore-environment sh -c 'test -n \"$HOME\"'", False),
        ("unset HOME; sh -c 'test -n \"$HOME\"'", False),
        ("unset HOME; export HOME=/tmp; sh -c 'test -n \"$HOME\"'", True),
        ('unset -f HOME; set -u; test -n "$HOME"', True),
    ),
)
def test_ordered_shell_bind_and_unbind_effects_match_runtime(
    tmp_path: Path, command: str, passed: bool
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Ordered shell state", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed is passed
    environment = os.environ.copy()
    environment["HOME"] = "/host-home"
    environment.pop("FOO", None)
    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert (runtime.returncode == 0) is passed


@pytest.mark.parametrize("variable", ("TMPDIR", "SHELL", "USER"))
def test_preflight_uses_actual_optional_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.delenv(variable, raising=False)
    command = f'test -n "${variable}"'

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Host binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
    )

    runtime_passed = runtime.returncode == 0
    assert report.passed is runtime_passed


@pytest.mark.parametrize(
    "command",
    (
        'false && FOO=bar; test -n "$FOO"',
        'true || FOO=bar; test -n "$FOO"',
    ),
)
def test_conditional_assignments_are_not_guaranteed_bindings(tmp_path: Path, command: str) -> None:
    environment = os.environ.copy()
    environment.pop("FOO", None)
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Conditional binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert report.passed is False
    assert runtime.returncode != 0


def test_empty_environment_shell_initializes_path(tmp_path: Path) -> None:
    command = "env -i /bin/sh -c 'test -n \"$PATH\"'"
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Shell default", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
    )

    assert report.passed is True
    assert runtime.returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "env -i sh -c 'set -u; printf %s \"$PPID\"'",
        "env -i sh -c 'cd /; set -u; printf %s \"$OLDPWD\"'",
        "env -i bash -c 'set -u; printf %s \"$BASH_VERSION\"'",
    ),
)
def test_shell_created_bindings_match_empty_environment_runtime(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Shell-created binding", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
    )

    assert report.passed
    assert runtime.returncode == 0


@pytest.mark.parametrize(
    ("command", "passed"),
    (
        ("for FOO in; do :; done; set -u; printf '%s' \"$FOO\"", False),
        ("if true; then FOO=bar; fi; set -u; printf '%s' \"$FOO\"", True),
        ("{ FOO=bar; }; set -u; printf '%s' \"$FOO\"", True),
        ("set_foo() { FOO=bar; }; set_foo; set -u; printf '%s' \"$FOO\"", True),
        ("read FOO <<'EOF'\nbar\nEOF\nset -u; test \"$FOO\" = bar", True),
        (
            'unset FOO; printf %s "$(printf %s "${FOO:=bar}")"; set -u; test -n "$FOO"',
            False,
        ),
        ('unset FOO; printf x | printf %s "${FOO:=bar}"; set -u; test -n "$FOO"', False),
        ('unset FOO; set -u; test "${#FOO}" -eq 0', False),
        ("printf ok # $MISSING", True),
    ),
)
def test_complex_shell_state_and_comments_match_runtime(
    tmp_path: Path, command: str, passed: bool
) -> None:
    environment = os.environ.copy()
    environment.pop("FOO", None)
    environment.pop("MISSING", None)
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Shell grammar", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert report.passed is passed
    assert (runtime.returncode == 0) is passed


@pytest.mark.parametrize(
    "payload",
    (
        'printf -v FOO x; set -u; test "$FOO" = x',
        '((FOO = 1)); set -u; test "$FOO" = 1',
        'let FOO=1; set -u; test "$FOO" = 1',
    ),
)
def test_unsupported_bash_state_mutation_is_inconclusive(tmp_path: Path, payload: str) -> None:
    command = f"bash -c {shlex.quote(payload)}"
    environment = os.environ.copy()
    environment.pop("FOO", None)

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Bash mutation", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert (
        subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env=environment,
            check=False,
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    "command",
    (
        "FOO=bar | printf '%s' \"$FOO\"",
        "FOO=bar & printf '%s' \"$FOO\"",
        "export FOO=bar | cat; printf '%s' \"$FOO\"",
        "export FOO=bar |& cat; printf '%s' \"$FOO\"",
        "FOO=bar env -u FOO sh -c 'printf %s \"$FOO\"'",
    ),
)
def test_shell_subprocess_bindings_do_not_leak_or_precede_expansion(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Scoped binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.subject for finding in report.blocking_findings] == ["$FOO"]


@pytest.mark.parametrize(
    "command",
    (
        "command -v ./missing-verifier",
        "command -V ./missing-verifier",
        "! command -v ./missing-verifier",
        "! command -V ./missing-verifier",
        "command -p -v -- ./missing-verifier",
    ),
)
def test_command_resolution_queries_do_not_execute_their_operands(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Command query", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert all(finding.code != "verify_program_missing" for finding in report.findings)


def test_command_execution_option_still_exposes_missing_program(tmp_path: Path) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Wrapped verifier",
                    verify_command="command -p -- ./missing-verifier",
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]


@pytest.mark.parametrize(
    "command",
    (
        "command -- ./missing-verify",
        "exec ./missing-verify",
        "command exec ./missing-verify",
        "nice -n5 ./missing-verify",
        "nice --adjustment=5 ./missing-verify",
        "nice -- ./missing-verify",
        "nice -n 5 -- ./missing-verify",
        "printf ok\n./missing-verifier.sh",
        ". ./missing-verifier.sh",
    ),
)
def test_supported_wrapper_option_forms_preserve_program_position(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]

    runtime = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
    )
    assert runtime.returncode != 0


def test_environment_assignment_after_use_does_not_bind_earlier_expansion(tmp_path: Path) -> None:
    seed = _seed(
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="Ordered shell expansion",
                verify_command='echo "$TOKEN"; TOKEN=x true',
            ),
        )
    )

    report = run_seed_preflight(seed, workspace_root=tmp_path)

    assert [finding.subject for finding in report.blocking_findings] == ["$TOKEN"]


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


@pytest.mark.parametrize(
    "command",
    (
        "cd sub && python check.py",
        "cd -- sub && python check.py",
        "CDPATH= cd sub && python check.py",
        "CDPATH= command cd sub && python check.py",
        "env -C sub python check.py",
        "env --chdir=sub python check.py",
        "sh -c 'cd sub && python check.py'",
        "sh -eu -c 'cd sub && python check.py'",
        "bash -O extglob -c 'cd sub && python check.py'",
        "bash --rcfile /dev/null -c 'cd sub && python check.py'",
        "cd sub && bash -c 'python check.py'",
        "env -C sub bash -c 'python check.py'",
        "if true; then cd sub; fi; python check.py",
        "{ cd sub; }; python check.py",
    ),
)
def test_verifier_program_resolves_from_deterministic_shell_directory(
    tmp_path: Path, command: str
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "check.py").write_text("print('ok')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Directory-aware verifier", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    if "--chdir=" not in command or os.uname().sysname != "Darwin":
        assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "cd sub && python check.py",
        "cd -- sub && python check.py",
        "CDPATH= cd sub && python check.py",
        "CDPATH= command cd sub && python check.py",
        "env -C sub python check.py",
        "env --chdir=sub python check.py",
        "sh -c 'cd sub && python check.py'",
        "sh -eu -c 'cd sub && python check.py'",
        "bash -O extglob -c 'cd sub && python check.py'",
        "bash --rcfile /dev/null -c 'cd sub && python check.py'",
        "cd sub && bash -c 'python check.py'",
        "env -C sub bash -c 'python check.py'",
        "if true; then cd sub; fi; python check.py",
        "{ cd sub; }; python check.py",
    ),
)
def test_verifier_program_missing_in_effective_shell_directory_is_blocked(
    tmp_path: Path, command: str
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Directory-aware verifier", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(
    "command",
    (
        "cd 'sub dir' && python check.py",
        r"cd sub\ dir && python check.py",
        "env -C 'sub dir' python check.py",
        r"env -C sub\ dir python check.py",
        'env --chdir="sub dir" python check.py',
        "sh -c \"cd 'sub dir' && python check.py\"",
        r"sh -c 'cd sub\ dir && python check.py'",
        "cd 'sub dir' && sh -c 'python check.py'",
        r"env -C sub\ dir sh -c 'python check.py'",
    ),
)
def test_shell_decoded_working_directory_matches_production_shell(
    tmp_path: Path, command: str
) -> None:
    sub = tmp_path / "sub dir"
    sub.mkdir()
    (sub / "check.py").write_text("print('ok')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Shell-word-aware directory", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    if "--chdir=" not in command or os.uname().sysname != "Darwin":
        assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "cd 'sub dir' && python check.py",
        r"cd sub\ dir && python check.py",
        "env -C 'sub dir' python check.py",
        r"env -C sub\ dir python check.py",
        'env --chdir="sub dir" python check.py',
        "sh -c \"cd 'sub dir' && python check.py\"",
        r"sh -c 'cd sub\ dir && python check.py'",
        "cd 'sub dir' && sh -c 'python check.py'",
        r"env -C sub\ dir sh -c 'python check.py'",
    ),
)
def test_shell_decoded_missing_program_matches_production_shell(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "sub dir").mkdir()
    (tmp_path / "check.py").write_text("print('wrong directory')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Shell-word-aware directory", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    if "--chdir=" not in command or os.uname().sysname != "Darwin":
        assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(
    "command",
    (
        'python "missing verifier.py"',
        "python 'missing verifier.py'",
        r"python missing\ verifier.py",
    ),
)
def test_quoted_or_escaped_verifier_operand_is_blocked(tmp_path: Path, command: str) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Quoted verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(
    "command",
    (
        'python "existing verifier.py"',
        "python 'existing verifier.py'",
        r"python existing\ verifier.py",
    ),
)
def test_quoted_or_escaped_existing_verifier_operand_matches_shell(
    tmp_path: Path, command: str
) -> None:
    verifier = tmp_path / "existing verifier.py"
    verifier.write_text("print('ok')\n", encoding="utf-8")
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Quoted verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_root_artifact_does_not_satisfy_program_in_effective_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Directory-aware generated verifier",
                    verify_command="cd sub && python check.py",
                    expected_artifacts=("check.py",),
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]


@pytest.mark.parametrize(
    "command",
    ("cd missing; python check.py", "false && cd sub; python check.py"),
)
def test_failed_or_skipped_cd_is_inconclusive_for_later_verifier(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Conditional directory", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_program_classification_is_scoped_to_each_token_occurrence(tmp_path: Path) -> None:
    command = "cd sub; printf check.py; cd ..; python check.py"
    (tmp_path / "sub").mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Repeated filename", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    ("{ python missing.py; }", "if true; then python missing.py; fi", "( python missing.py )"),
)
def test_missing_program_in_compound_command_is_blocked(tmp_path: Path, command: str) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Compound verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


def test_negated_missing_program_is_still_blocked_with_shell_parity(tmp_path: Path) -> None:
    command = "! python missing.py"
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Negated verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]


@pytest.mark.parametrize(
    "command",
    (
        "if false; then :; else ! ./missing-verifier; fi",
        "if ! true; then :; else ! ./missing-verifier; fi",
    ),
)
def test_negated_missing_program_in_guaranteed_else_branch_blocks_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="verify", verify_command=command),
            ),
        ),
        workspace_root=tmp_path,
    )

    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0
    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]


@pytest.mark.parametrize(
    "command",
    (
        "set -a; readonly FOO=bar; sh -uc 'test \"$FOO\" = bar'",
        "set -a; read FOO <<'EOF'\nbar\nEOF\nsh -uc 'test \"$FOO\" = bar'",
        "set -a; getopts x FOO -x; sh -uc 'test \"$FOO\" = x'",
    ),
)
def test_allexport_special_builtin_bindings_match_posix_shell(tmp_path: Path, command: str) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="verify", verify_command=command),
            ),
        ),
        workspace_root=tmp_path,
    )

    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0
    assert not report.blocking_findings
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_negated_nohup_missing_program_is_blocked_with_shell_parity(tmp_path: Path) -> None:
    command = "! nohup ./definitely-missing.sh >/dev/null 2>&1"
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Negated nohup verifier",
                    verify_command=command,
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "! time ./definitely-missing-verifier",
        "! /usr/bin/time -p ./definitely-missing-verifier",
    ),
)
def test_negated_time_missing_program_is_blocked_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Negated time verifier",
                    verify_command=command,
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "! stdbuf -oL ./definitely-missing-verifier",
        "! stdbuf -i0 -o0 -eL ./definitely-missing-verifier",
        "! stdbuf --output=L ./definitely-missing-verifier",
        "! stdbuf -o 0 -- ./definitely-missing-verifier",
    ),
)
def test_negated_stdbuf_missing_program_is_blocked_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    """stdbuf wraps a command; a negated missing verifier must still block."""
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Negated stdbuf verifier",
                    verify_command=command,
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "! setsid ./definitely-missing-verifier",
        "! setsid --wait ./definitely-missing-verifier",
        "! setsid -w ./definitely-missing-verifier",
        "! setsid -- ./definitely-missing-verifier",
    ),
)
def test_negated_setsid_missing_program_is_blocked_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    """setsid wraps a command; a negated missing verifier must still block.

    Note: setsid may not be installed on macOS, but the preflight parser
    must still detect the missing verifier regardless of local availability.
    The /bin/sh parity assertion is skipped when setsid is not on PATH because
    sh itself will report 'command not found' for setsid (non-zero), while
    on Linux where setsid exists the negated missing program returns 0.
    """
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Negated setsid verifier",
                    verify_command=command,
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    # On systems with setsid, the negated command exits 0 — proving the
    # preflight gate is essential.  On systems without setsid, sh itself
    # fails, so the parity assertion only applies when setsid is available.
    import shutil

    if shutil.which("setsid"):
        assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "stdbuf -oL ./missing-verify",
        "stdbuf --output=0 -- ./missing-verify",
        "setsid ./missing-verify",
        "setsid --wait -- ./missing-verify",
    ),
)
def test_setsid_stdbuf_wrapper_option_forms_preserve_program_position(
    tmp_path: Path, command: str
) -> None:
    """setsid/stdbuf wrappers must expose the wrapped program for preflight."""
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Wrapper verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )
    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]


@pytest.mark.parametrize(
    "command",
    (
        "if false; then ./missing; fi; true",
        "false && ./missing; true",
        "true || ./missing; true",
    ),
)
def test_statically_unreachable_programs_do_not_block_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Skipped verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    (
        "if test -f absent.marker; then ./missing; fi; true",
        "test -f absent.marker && ./missing; true",
        "while test -f absent.marker; do ./missing; done; true",
    ),
)
def test_runtime_dependent_program_reachability_is_inconclusive_with_shell_parity(
    tmp_path: Path, command: str
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Conditional verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert not report.blocking_findings
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    ("command", "blocked"),
    (
        ("if test -f absent.marker; then true; fi; python missing.py", True),
        ("if ! true; then ./missing; fi; true", False),
        ("for x in; do ./missing; done; true", False),
    ),
)
def test_program_reachability_is_scoped_per_occurrence_with_shell_parity(
    tmp_path: Path, command: str, blocked: bool
) -> None:
    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Scoped verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert bool(report.blocking_findings) is blocked
    result = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False)
    assert (result.returncode != 0) is blocked


def test_prior_cd_failure_exit_does_not_hide_later_missing_verifier(tmp_path: Path) -> None:
    command = "cd . || exit 1; python3 missing.py"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Guarded cd", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(
    "command",
    ("python3 check.py && cd sub", "python3 check.py && env -C sub true"),
)
def test_later_directory_change_does_not_rebase_earlier_verifier(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Ordered directory verifier", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize(
    "command",
    ("python3 check.py && cd sub", "python3 check.py && env -C sub true"),
)
def test_later_directory_change_cannot_hide_missing_earlier_verifier(
    tmp_path: Path, command: str
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "check.py").write_text("print('ok')\n", encoding="utf-8")

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Ordered directory verifier", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize(("program_location", "passed"), (("home", True), ("workspace", False)))
def test_bare_cd_resolves_programs_from_home_like_bin_sh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    program_location: str,
    passed: bool,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    (home if program_location == "home" else workspace).joinpath("check.py").write_text(
        "print('ok')\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    command = "cd && python check.py"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Bare cd", verify_command=command),
            )
        ),
        workspace_root=workspace,
    )

    assert report.passed is passed
    runtime = subprocess.run(["sh", "-c", command], cwd=workspace, check=False)
    assert (runtime.returncode == 0) is passed


@pytest.mark.parametrize("builtin", ("declare", "local", "typeset"))
def test_bash_assignment_builtins_do_not_bind_in_outer_posix_shell(
    tmp_path: Path, builtin: str
) -> None:
    dash = Path("/bin/dash")
    if not dash.exists():
        pytest.skip("dash is required for strict POSIX-shell comparison")
    command = f'{builtin} FOO=bar; set -u; test -n "$FOO"'

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="POSIX shell binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.subject for finding in report.blocking_findings] == ["$FOO"]
    assert subprocess.run([dash, "-c", command], cwd=tmp_path, check=False).returncode != 0


@pytest.mark.parametrize("builtin", ("declare", "typeset"))
def test_bash_assignment_builtins_bind_inside_explicit_bash_payload(
    tmp_path: Path, builtin: str
) -> None:
    command = f"bash -c '{builtin} FOO=bar; set -u; test -n \"$FOO\"'"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Bash shell binding", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


@pytest.mark.parametrize("operation", ("export -n HOME", "declare +x HOME"))
def test_bash_deexport_is_removed_from_nested_shell_scope(tmp_path: Path, operation: str) -> None:
    payload = f"{operation}; sh -c 'set -u; test -n \"$HOME\"'"
    command = shlex.join(("bash", "-c", payload))

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Bash de-export", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.subject for finding in report.blocking_findings] == ["$HOME"]
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


def test_env_chdir_is_local_to_its_wrapped_command(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    command = "env -C sub true; python check.py"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Command-local env directory", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_env_chdir_does_not_rebase_later_verifier(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "check.py").write_text("print('ok')\n", encoding="utf-8")
    command = "env -C sub true; python check.py"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Command-local env directory", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


def test_relative_cd_changes_accumulate_in_execution_order(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    command = "cd sub; cd ..; python check.py"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Cumulative directory state", verify_command=command
                ),
            )
        ),
        workspace_root=tmp_path,
    )

    assert report.passed
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode == 0


def test_repeated_verifier_is_rechecked_after_directory_change(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    command = "python check.py; cd sub; python check.py"

    report = run_seed_preflight(
        _seed(
            acceptance_criteria=(
                AcceptanceCriterionSpec(description="Repeated verifier", verify_command=command),
            )
        ),
        workspace_root=tmp_path,
    )

    assert [finding.code for finding in report.blocking_findings] == ["verify_program_missing"]
    assert subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False).returncode != 0


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
