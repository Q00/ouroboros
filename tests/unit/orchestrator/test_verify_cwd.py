from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.orchestrator.verify_cwd import (
    resolve_verify_command_cwd as _resolve_verify_command_cwd,
)
from tests.unit.orchestrator.test_parallel_executor_verify_by_default import _make_executor


@pytest.mark.asyncio
async def test_verify_gate_runs_command_in_explicit_verify_cwd(tmp_path: Any) -> None:
    (tmp_path / "app").mkdir()
    command = (
        "python3 -c \"import pathlib, sys; sys.exit(0 if pathlib.Path.cwd().name == 'app' else 3)\""
    )
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ac", verify_command=command, verify_cwd="app")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True


@pytest.mark.asyncio
async def test_verify_gate_fails_loud_when_verify_cwd_missing(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ac", verify_command="exit 0", verify_cwd="app")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert "verify_cwd does not exist" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_verify_gate_rejects_verify_cwd_symlink_escape(tmp_path: Any) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").symlink_to(outside, target_is_directory=True)
    executor = _make_executor(working_directory=str(workspace))
    spec = AcceptanceCriterionSpec(description="ac", verify_command="exit 0", verify_cwd="app")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))

    assert outcome.passed is False
    assert "escapes the workspace" in (outcome.reason or "")


def test_resolve_verify_cwd_uses_sole_node_manifest_directory(tmp_path: Any) -> None:
    """Greenfield runs frequently scaffold the app in one subdirectory the
    seed could not know in advance (incident exec_418f29f31e5c: root
    ``npx playwright test`` while the config lived in ``app/``)."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command="npx playwright test")

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert Path(resolved) == (tmp_path / "app").resolve()


@pytest.mark.parametrize("verify_cwd", ["alias", "alias/app"])
def test_binding_rejects_root_alias_paths(tmp_path: Path, verify_cwd: str) -> None:
    from ouroboros.orchestrator.verify_cwd import bind_verify_command_cwd

    root = tmp_path.resolve()
    (root / "app").mkdir()
    (root / "alias").symlink_to(root, target_is_directory=True)
    spec = AcceptanceCriterionSpec(
        description="reject root alias", verify_command="exit 0", verify_cwd=verify_cwd
    )
    bound = bind_verify_command_cwd(str(root), spec)
    try:
        assert bound.error is not None
        assert "could not be securely bound" in bound.error
        assert bound.capability is None
    finally:
        bound.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_cwd", ["alias", "alias/app"])
async def test_verify_gate_rejects_root_alias_paths(tmp_path: Path, verify_cwd: str) -> None:
    root = tmp_path.resolve()
    (root / "app").mkdir()
    (root / "alias").symlink_to(root, target_is_directory=True)
    spec = AcceptanceCriterionSpec(
        description="reject root alias",
        verify_command="printf ROOT_ALIAS_ACCEPTED",
        verify_cwd=verify_cwd,
    )
    outcome = await _make_executor(working_directory=str(root))._run_ac_verify_gate(
        spec=spec, cwd=str(root)
    )
    assert outcome.passed is False
    assert "could not be securely bound" in (outcome.reason or "")
    assert "ROOT_ALIAS_ACCEPTED" not in (outcome.output_tail or "")


@pytest.mark.parametrize(
    "command",
    [
        "CI=1 npm test",
        "env CI=1 npx playwright test",
        "CI=1 env -i npx playwright test",
        "env -u HOME npm test",
        "env --unset=HOME yarn test",
        "env -- npx playwright test",
        "env --default-signal npm test",
        "env --ignore-signal=PIPE yarn test",
        "env --block-signal=PIPE pnpm test",
        "env --list-signal-handling npm test",
    ],
)
def test_resolve_verify_cwd_parses_environment_prefixed_node_command(
    tmp_path: Any,
    command: str,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command=command)

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert Path(resolved) == (tmp_path / "app").resolve()


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'npm test'",
        "sh -c 'npx playwright test'",
        "/bin/bash -lc 'pnpm test'",
        "command npm test",
    ],
)
def test_resolve_verify_cwd_parses_shell_wrapped_node_command(
    tmp_path: Any,
    command: str,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command=command)

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert Path(resolved) == (tmp_path / "app").resolve()


@pytest.mark.parametrize(
    "command",
    [
        "env CI=1 bash -lc 'npx playwright test'",
        "env CI=1 sh -c 'npm test'",
        "CI=1 bash -lc 'yarn test'",
        "CI=1 sh -c 'npm test'",
        "env CI=1 /bin/bash -lc 'pnpm test'",
        "env -i CI=1 bash -lc 'npm test'",
        "env -u HOME sh -c 'npx playwright test'",
    ],
)
def test_resolve_verify_cwd_parses_environment_prefixed_shell_wrapped_node_command(
    tmp_path: Any,
    command: str,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command=command)

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert Path(resolved) == (tmp_path / "app").resolve()


def test_resolve_verify_cwd_never_guesses_between_manifests(tmp_path: Any) -> None:
    for name in ("app", "site"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command="npm test")

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert resolved == str(tmp_path)


def test_resolve_verify_cwd_prefers_root_manifest(tmp_path: Any) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command="npm test")

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert resolved == str(tmp_path)


def test_resolve_verify_cwd_ignores_non_node_commands_and_node_modules(tmp_path: Any) -> None:
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}", encoding="utf-8")

    pytest_spec = AcceptanceCriterionSpec(description="ac", verify_command="pytest -q")
    resolved, error = _resolve_verify_command_cwd(str(tmp_path), pytest_spec)
    assert error is None
    assert resolved == str(tmp_path)

    node_spec = AcceptanceCriterionSpec(description="ac", verify_command="npm test")
    resolved, error = _resolve_verify_command_cwd(str(tmp_path), node_spec)
    assert error is None
    assert Path(resolved) == (tmp_path / "app").resolve()


def test_explicit_verify_cwd_wins_over_manifest_detection(tmp_path: Any) -> None:
    for name in ("app", "site"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "package.json").write_text("{}", encoding="utf-8")
    spec = AcceptanceCriterionSpec(description="ac", verify_command="npm test", verify_cwd="site")

    resolved, error = _resolve_verify_command_cwd(str(tmp_path), spec)

    assert error is None
    assert Path(resolved) == (tmp_path / "site").resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("projects", [("app",), ("app", "site")])
async def test_node_cwd_inference_runs_only_unambiguous_project(
    tmp_path: Path, projects: tuple[str, ...]
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    for project in projects:
        (workspace / project).mkdir()
        (workspace / project / "package.json").write_text("{}")
    runner = tmp_path / "npm"
    runner.write_text("#!/bin/sh\ntest -f package.json\n")
    runner.chmod(0o755)
    spec = AcceptanceCriterionSpec(description="project exists", verify_command=f"'{runner}' test")

    outcome = await _make_executor(working_directory=str(workspace))._run_ac_verify_gate(
        spec=spec, cwd=str(workspace)
    )

    assert outcome.passed is (len(projects) == 1)
    if len(projects) > 1:
        assert outcome.cause == "exit_nonzero"


@pytest.mark.asyncio
async def test_wrong_existing_cwd_does_not_accept_evidence(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "site").mkdir()
    (tmp_path / "app" / "proof.txt").write_text("proof")
    spec = AcceptanceCriterionSpec(
        description="app proof exists", verify_command="test -f proof.txt", verify_cwd="site"
    )
    outcome = await _make_executor(working_directory=str(tmp_path))._run_ac_verify_gate(
        spec=spec, cwd=str(tmp_path)
    )
    assert outcome.passed is False
    assert outcome.cause == "exit_nonzero"


@pytest.mark.asyncio
async def test_shell_cwd_preamble_remains_root_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "package.json").write_text("{}")
    runner = tmp_path / "npm"
    runner.write_text("#!/bin/sh\ntest -f package.json\n")
    runner.chmod(0o755)
    spec = AcceptanceCriterionSpec(
        description="nested manifest exists", verify_command=f'sh -c "cd app && {runner} test"'
    )
    outcome = await _make_executor(working_directory=str(workspace))._run_ac_verify_gate(
        spec=spec, cwd=str(workspace)
    )
    assert outcome.passed is True


@pytest.mark.parametrize("prefix", ["env --chdir app", "env -C app", "env --unknown"])
def test_cwd_changing_or_unknown_env_options_prevent_inference(tmp_path: Path, prefix: str) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}")
    spec = AcceptanceCriterionSpec(description="tests pass", verify_command=f"{prefix} npm test")
    assert _resolve_verify_command_cwd(str(tmp_path), spec) == (str(tmp_path), None)


@pytest.mark.parametrize(
    "command",
    [
        "env -S 'CI=1 pnpm test'",
        "env -S 'CI=1 bash -lc \"yarn test\"'",
        "env --split-string='npm test'",
        "env -S'npm test'",
    ],
)
def test_env_split_string_preserves_root_execution(tmp_path: Path, command: str) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}")
    spec = AcceptanceCriterionSpec(description="tests pass", verify_command=command)
    assert _resolve_verify_command_cwd(str(tmp_path), spec) == (str(tmp_path), None)


@pytest.mark.asyncio
async def test_explicit_root_cwd_overrides_node_inference(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "package.json").write_text("{}")
    (workspace / "root-marker").write_text("root")
    runner = tmp_path / "npm"
    runner.write_text("#!/bin/sh\ntest -f root-marker\n")
    runner.chmod(0o755)
    executor = _make_executor(working_directory=str(workspace))
    for cwd, passed in [(None, False), (".", True)]:
        spec = AcceptanceCriterionSpec(
            description="root marker exists", verify_command=f"'{runner}' test", verify_cwd=cwd
        )
        outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))
        assert outcome.passed is passed


@pytest.mark.parametrize("verify_cwd", [None, "app"])
def test_nonroot_cwd_fails_closed_without_directory_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verify_cwd: str | None
) -> None:
    from ouroboros.orchestrator import verify_cwd as resolver

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}")
    monkeypatch.setattr(resolver, "nofollow_directory_capabilities_available", lambda: False)
    bound = resolver.bind_verify_command_cwd(
        str(tmp_path),
        AcceptanceCriterionSpec(
            description="tests pass", verify_command="npm test", verify_cwd=verify_cwd
        ),
    )
    assert bound.capability is None
    assert bound.error == "secure verify_cwd directory binding is unavailable on this platform"


@pytest.mark.parametrize(
    "command",
    [
        "npm --prefix app test",
        "npm --prefix=app test",
        "npx --prefix app playwright test",
        "yarn --cwd app test",
        "yarn --cwd=app test",
        "pnpm --dir app test",
        "pnpm --dir=app test",
        "pnpm -C app test",
        "pnpm -Capp test",
        "env CI=1 bash -lc 'npm --prefix app test'",
    ],
)
def test_runner_cwd_options_prevent_inference(tmp_path: Path, command: str) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}")
    spec = AcceptanceCriterionSpec(description="tests pass", verify_command=command)
    assert _resolve_verify_command_cwd(str(tmp_path), spec) == (str(tmp_path), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_name, option", [("npm", "--prefix"), ("yarn", "--cwd"), ("pnpm", "--dir")]
)
async def test_runner_own_cwd_option_is_not_applied_twice(
    tmp_path: Path, runner_name: str, option: str
) -> None:
    workspace = tmp_path / "work"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "package.json").write_text("{}")
    runner = tmp_path / runner_name
    runner.write_text('#!/bin/sh\ncd "$2" && test -f package.json\n')
    runner.chmod(0o755)
    spec = AcceptanceCriterionSpec(
        description="nested manifest exists", verify_command=f"'{runner}' {option} app test"
    )
    outcome = await _make_executor(working_directory=str(workspace))._run_ac_verify_gate(
        spec=spec, cwd=str(workspace)
    )
    assert outcome.passed is True


@pytest.mark.asyncio
async def test_nonroot_cwd_keeps_artifacts_and_digest_rooted(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "proof.txt").write_text("root proof")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="root proof exists",
        verify_command="test -f ../proof.txt",
        verify_cwd="app",
        expected_artifacts=("proof.txt",),
    )
    before = executor._workspace_content_digest(str(tmp_path), expected_artifacts=("proof.txt",))
    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))
    assert outcome.passed is True
    assert outcome.workspace_digest == before


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["proof.txt", "../proof.txt"])
async def test_nonroot_cwd_detects_same_content_mutation(tmp_path: Path, target: str) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "proof.txt").write_text("root proof")
    (tmp_path / "app" / "proof.txt").write_text("nested proof")
    spec = AcceptanceCriterionSpec(
        description="proof unchanged",
        verify_cwd="app",
        verify_command=f"python3 -c \"from pathlib import Path; p=Path('{target}'); p.write_bytes(p.read_bytes())\"",
    )
    outcome = await _make_executor(working_directory=str(tmp_path))._run_ac_verify_gate(
        spec=spec, cwd=str(tmp_path)
    )
    assert outcome.passed is False
    assert outcome.workspace_mutated is True
