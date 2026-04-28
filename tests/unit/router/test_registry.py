from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil

import pytest

from ouroboros.router import (
    MCPDispatchTarget,
    NoMatchReason,
    NotHandled,
    ParsedOooCommand,
    Resolved,
    ResolveRequest,
    SkillDispatchRegistry,
    SkillDispatchTarget,
    SkillDispatchTargetResolution,
    normalize_skill_identifier,
    packaged_skill_dispatch_registry,
    resolve_skill_dispatch,
    resolve_skill_dispatch_target,
)


def _write_skill(skills_dir: Path, skill_name: str, frontmatter: str) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter}---\n# {skill_name}\n", encoding="utf-8")
    return skill_md_path


def test_registry_maps_direct_names_and_aliases_to_canonical_targets(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    run_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
aliases:
  - start
  - ooo launch
command_aliases: "dispatch, /ouroboros:ship"
""",
    )
    execute_path = _write_skill(
        skills_dir,
        "execute",
        """\
name: execute
""",
    )

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)

    assert registry.targets == (
        SkillDispatchTarget(
            skill_name="execute",
            skill_path=execute_path,
            identifiers=("execute",),
        ),
        SkillDispatchTarget(
            skill_name="run",
            skill_path=run_path,
            identifiers=("run", "execute", "start", "launch", "dispatch", "ship"),
        ),
    )
    run = registry.resolve("run")
    start = registry.resolve("start")
    launch = registry.resolve("OOO launch")
    ship = registry.resolve("/ouroboros:ship")
    execute = registry.resolve("execute")

    assert isinstance(run, SkillDispatchTarget)
    assert isinstance(start, SkillDispatchTarget)
    assert isinstance(launch, SkillDispatchTarget)
    assert isinstance(ship, SkillDispatchTarget)
    assert isinstance(execute, SkillDispatchTarget)
    assert run.skill_name == "run"
    assert start.skill_name == "run"
    assert launch.skill_name == "run"
    assert ship.skill_name == "run"
    assert execute.skill_name == "execute"


def test_registry_normalizes_canonical_alias_arrays(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_path = _write_skill(
        skills_dir,
        "run",
        """\
name: Execute
alias:
  - Quick-Run
aliases:
  - start
  - ooo launch seed.yaml
command_aliases:
  - /ouroboros:ship
  - dispatch
skill_aliases:
  - OOO Ship-It
commands:
  - /ouroboros:go
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)

    expected = SkillDispatchTarget(
        skill_name="run",
        skill_path=skill_path,
        identifiers=(
            "run",
            "execute",
            "quick-run",
            "start",
            "launch",
            "ship",
            "dispatch",
            "ship-it",
            "go",
        ),
    )
    assert registry.targets == (expected,)
    assert registry.resolve("OoO Quick-Run") == expected
    assert registry.resolve("/OUROBOROS:Ship") == expected
    assert registry.resolve("ooo go seed.yaml") == expected


def test_registry_ignores_empty_and_missing_alias_metadata(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    empty_path = _write_skill(
        skills_dir,
        "empty",
        """\
alias: ""
aliases: []
command_aliases: ""
skill_aliases: null
commands:
  - ""
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )
    missing_path = _write_skill(
        skills_dir,
        "missing",
        """\
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)

    assert registry.targets == (
        SkillDispatchTarget(
            skill_name="empty",
            skill_path=empty_path,
            identifiers=("empty",),
        ),
        SkillDispatchTarget(
            skill_name="missing",
            skill_path=missing_path,
            identifiers=("missing",),
        ),
    )
    assert tuple(registry.mapping) == ("empty", "missing")


def test_registry_deduplicates_aliases_preserving_first_normalized_order(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_path = _write_skill(
        skills_dir,
        "run",
        """\
name: Run
alias: ooo start seed.yaml
aliases:
  - start
  - /ouroboros:start
  - START
command_aliases: "start, ooo begin, begin"
skill_aliases:
  - begin
commands:
  - /ouroboros:go
  - go
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)

    expected = SkillDispatchTarget(
        skill_name="run",
        skill_path=skill_path,
        identifiers=("run", "start", "begin", "go"),
    )
    assert registry.targets == (expected,)
    assert registry.mapping == {
        "run": expected,
        "start": expected,
        "begin": expected,
        "go": expected,
    }


def test_normalize_skill_identifier_accepts_parsed_and_prefixed_identifiers() -> None:
    assert normalize_skill_identifier(" Run ") == "run"
    assert normalize_skill_identifier("ooo Evaluate seed.yaml") == "evaluate"
    assert normalize_skill_identifier("/OUROBOROS:status orch_123") == "status"
    assert normalize_skill_identifier("run!") is None
    assert normalize_skill_identifier("/ouroboros:") is None


@pytest.mark.parametrize(
    "raw_identifier",
    [
        pytest.param("", id="empty"),
        pytest.param("   \t", id="whitespace-only"),
        pytest.param("ooo ! seed.yaml", id="invalid-ooo-target"),
        pytest.param("/ouroboros:", id="missing-slash-target"),
        pytest.param("/ouroboros:/run seed.yaml", id="invalid-slash-target"),
        pytest.param("run!", id="invalid-direct-identifier"),
    ],
)
def test_skill_lookup_identifier_normalization_returns_empty_for_no_match_inputs(
    raw_identifier: str,
) -> None:
    assert normalize_skill_identifier(raw_identifier) is None


def test_router_resolves_alias_to_one_stable_skill_target(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
aliases:
  - start
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
""",
    )

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo start seed.yaml",
            cwd=tmp_path,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == "ooo start"
    assert result.skill_path == skill_path
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {"seed_path": "seed.yaml", "cwd": str(tmp_path)}


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            "ooo evaluate reports/final.md",
            "ooo evaluate",
            id="ooo-exact-skill-name",
        ),
        pytest.param(
            "/ouroboros:evaluate reports/final.md",
            "/ouroboros:evaluate",
            id="slash-exact-skill-name",
        ),
    ],
)
def test_router_skill_lookup_exact_skill_name_returns_expected_dispatch_result(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "run",
        """\
name: execute
aliases:
  - evaluate
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
""",
    )
    skill_path = _write_skill(
        skills_dir,
        "evaluate",
        """\
name: review
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  cwd: "$CWD"
""",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
    )

    expected_args = {
        "artifact": "reports/final.md",
        "cwd": str(runtime_cwd),
    }
    assert isinstance(result, Resolved)
    assert result.skill_name == "evaluate"
    assert result.command_prefix == expected_prefix
    assert result.prompt == prompt
    assert result.skill_path == skill_path
    assert result.first_argument == "reports/final.md"
    assert result.mcp_tool == "ouroboros_evaluate"
    assert result.mcp_args == expected_args
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_evaluate",
        mcp_args=expected_args,
    )


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            "ooo run seed.yaml",
            "ooo run",
            id="ooo-direct-skill-name",
        ),
        pytest.param(
            "/ouroboros:run seed.yaml",
            "/ouroboros:run",
            id="slash-direct-skill-name",
        ),
    ],
)
def test_router_skill_lookup_ambiguous_direct_name_wins_over_alias(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "alpha",
        """\
aliases:
  - run
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
""",
    )
    skill_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  cwd: "$CWD"
""",
    )
    runtime_cwd = tmp_path / "workspace"

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)
    target = registry.resolve("run")
    result = resolve_skill_dispatch(
        ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
    )

    assert target == SkillDispatchTarget(
        skill_name="run",
        skill_path=skill_path,
        identifiers=("run", "execute"),
    )
    expected_args = {
        "artifact": "seed.yaml",
        "cwd": str(runtime_cwd),
    }
    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == expected_prefix
    assert result.prompt == prompt
    assert result.skill_path == skill_path
    assert result.first_argument == "seed.yaml"
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_evaluate",
        mcp_args=expected_args,
    )


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            "ooo shared seed.yaml",
            "ooo shared",
            id="ooo-shared-alias",
        ),
        pytest.param(
            "/ouroboros:shared seed.yaml",
            "/ouroboros:shared",
            id="slash-shared-alias",
        ),
    ],
)
def test_router_skill_lookup_ambiguous_alias_uses_first_canonical_target(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    alpha_path = _write_skill(
        skills_dir,
        "alpha",
        """\
aliases:
  - shared
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  cwd: "$CWD"
""",
    )
    _write_skill(
        skills_dir,
        "zeta",
        """\
aliases:
  - shared
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
""",
    )
    runtime_cwd = tmp_path / "workspace"

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)
    target = registry.resolve("shared")
    result = resolve_skill_dispatch(
        ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
    )

    assert target == SkillDispatchTarget(
        skill_name="alpha",
        skill_path=alpha_path,
        identifiers=("alpha", "shared"),
    )
    expected_args = {
        "artifact": "seed.yaml",
        "cwd": str(runtime_cwd),
    }
    assert isinstance(result, Resolved)
    assert result.skill_name == "alpha"
    assert result.command_prefix == expected_prefix
    assert result.prompt == prompt
    assert result.skill_path == alpha_path
    assert result.first_argument == "seed.yaml"
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_evaluate",
        mcp_args=expected_args,
    )


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            "ooo execute seed.yaml",
            "ooo execute",
            id="frontmatter-name",
        ),
        pytest.param(
            "ooo quick-run seed.yaml",
            "ooo quick-run",
            id="alias-field",
        ),
        pytest.param(
            "ooo start seed.yaml",
            "ooo start",
            id="aliases-field",
        ),
        pytest.param(
            "ooo begin seed.yaml",
            "ooo begin",
            id="command-alias-ooo-value",
        ),
        pytest.param(
            "/ouroboros:launch seed.yaml",
            "/ouroboros:launch",
            id="command-alias-slash-value",
        ),
        pytest.param(
            "ooo ship-it seed.yaml",
            "ooo ship-it",
            id="skill-alias-prefixed-value",
        ),
        pytest.param(
            "/ouroboros:go seed.yaml",
            "/ouroboros:go",
            id="commands-field",
        ),
    ],
)
def test_router_skill_lookup_alias_matches_resolve_to_expected_canonical_skill(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
alias: Quick-Run
aliases:
  - start
command_aliases:
  - ooo begin
  - /ouroboros:launch
skill_aliases:
  - OOO Ship-It
commands:
  - /ouroboros:go
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
""",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
    )

    expected_args = {"seed_path": "seed.yaml", "cwd": str(runtime_cwd)}
    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == expected_prefix
    assert result.prompt == prompt
    assert result.skill_path == skill_path
    assert result.first_argument == "seed.yaml"
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )


def test_packaged_registry_target_resolver_uses_frontmatter_aliases(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_path = _write_skill(
        skills_dir,
        "evaluate",
        """\
aliases:
  - eval
mcp_tool: ouroboros_evaluate
mcp_args: {}
""",
    )

    with resolve_skill_dispatch_target("eval", skills_dir=skills_dir) as target:
        assert target == SkillDispatchTarget(
            skill_name="evaluate",
            skill_path=skill_path,
            identifiers=("evaluate", "eval"),
        )


def test_packaged_registry_target_resolver_keeps_resource_context_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills_dir = tmp_path / "ephemeral-skills"
    skill_path = skills_dir / "run" / "SKILL.md"

    @contextmanager
    def ephemeral_skills_dir(
        *,
        skills_dir: str | Path | None = None,
        anchor_file: str | Path,
        package: str = "ouroboros.skills",
    ):
        assert skills_dir is None
        assert anchor_file
        assert package == "ouroboros.skills"
        _write_skill(
            tmp_path / "ephemeral-skills",
            "run",
            """\
aliases:
  - execute
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
        )
        try:
            yield tmp_path / "ephemeral-skills"
        finally:
            shutil.rmtree(tmp_path / "ephemeral-skills")

    monkeypatch.setattr(
        "ouroboros.router.registry.resolve_packaged_skills_dir",
        ephemeral_skills_dir,
    )

    with resolve_skill_dispatch_target("execute") as target:
        assert target == SkillDispatchTarget(
            skill_name="run",
            skill_path=skill_path,
            identifiers=("run", "execute"),
        )
        assert target.skill_path.is_file()

    assert not skill_path.exists()


def test_registry_resolves_parsed_command_to_canonical_target(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_path = _write_skill(
        skills_dir,
        "run",
        """\
aliases:
  - start
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )
    parsed = ParsedOooCommand(
        skill_name="start",
        command_prefix="ooo start",
        remainder="seed.yaml",
    )

    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)
    registry_target = registry.resolve(parsed)
    expected = SkillDispatchTarget(
        skill_name="run",
        skill_path=skill_path,
        identifiers=("run", "start"),
    )

    with resolve_skill_dispatch_target(parsed, skills_dir=skills_dir) as packaged_target:
        assert packaged_target == expected

    assert registry_target == expected


def test_registry_skill_lookup_empty_directory_returns_empty_mapping_and_not_found(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)

    assert registry.targets == ()
    assert registry.mapping == {}

    for identifier in ("run", "ooo run seed.yaml", "/ouroboros:run seed.yaml"):
        result = registry.resolve(identifier)
        assert isinstance(result, NotHandled)
        assert result.reason == "skill not found"
        assert result.category is NoMatchReason.SKILL_NOT_FOUND
        assert result.outcome.value == "no_match"


def test_registry_missing_skill_returns_typed_not_handled(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "run",
        """\
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )
    registry = SkillDispatchRegistry.from_skills_dir(skills_dir)

    missing = registry.resolve("missing")
    invalid_identifier = registry.resolve("missing!")

    assert isinstance(missing, NotHandled)
    assert missing.reason == "skill not found"
    assert missing.category is NoMatchReason.SKILL_NOT_FOUND
    assert missing.outcome.value == "no_match"
    assert isinstance(invalid_identifier, NotHandled)
    assert invalid_identifier.category is NoMatchReason.SKILL_NOT_FOUND


@pytest.mark.parametrize(
    "identifier",
    [
        pytest.param("missing", id="missing-direct-identifier"),
        pytest.param("ooo missing seed.yaml", id="missing-ooo-identifier"),
        pytest.param("/ouroboros:missing seed.yaml", id="missing-slash-identifier"),
        pytest.param("run!", id="invalid-direct-identifier"),
        pytest.param("", id="empty-identifier"),
        pytest.param("   \t", id="whitespace-identifier"),
    ],
)
def test_packaged_registry_target_resolver_no_match_inputs_return_not_found(
    tmp_path: Path,
    identifier: str,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "run",
        """\
mcp_tool: ouroboros_execute_seed
mcp_args: {}
""",
    )

    with resolve_skill_dispatch_target(identifier, skills_dir=skills_dir) as result:
        assert isinstance(result, NotHandled)
        assert result.reason == "skill not found"
        assert result.category is NoMatchReason.SKILL_NOT_FOUND
        assert result.outcome.value == "no_match"


def test_packaged_registry_target_resolver_missing_skill_returns_not_handled(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    with resolve_skill_dispatch_target("missing", skills_dir=skills_dir) as result:
        assert isinstance(result, NotHandled)
        assert result.reason == "skill not found"
        assert result.category is NoMatchReason.SKILL_NOT_FOUND


def test_packaged_registry_target_resolver_type_alias_includes_not_handled() -> None:
    assert SkillDispatchTargetResolution.__value__ == (SkillDispatchTarget | NotHandled)


def test_packaged_registry_does_not_claim_claude_builtin_resume_command() -> None:
    with packaged_skill_dispatch_registry() as registry:
        reserved = registry.resolve("resume")
        renamed = registry.resolve("resume-session")

    assert isinstance(reserved, NotHandled)
    assert reserved.category is NoMatchReason.SKILL_NOT_FOUND
    assert isinstance(renamed, SkillDispatchTarget)
    assert renamed.skill_name == "resume-session"
