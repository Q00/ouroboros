from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import shutil
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from ouroboros.router import (
    DispatchTarget,
    DispatchTargetKind,
    InvalidInputReason,
    InvalidSkill,
    MCPDispatchTarget,
    McpDispatchTarget,
    NoMatchReason,
    NormalizedMCPFrontmatter,
    NormalizedMcpFrontmatter,
    NotHandled,
    ParsedOooCommand,
    Resolved,
    ResolveOutcome,
    ResolveRequest,
    ResolveResult,
    RouterRequest,
    SkillDispatchRouter,
    load_skill_frontmatter,
    normalize_mcp_frontmatter,
    resolve_dispatch_templates,
    resolve_parsed_skill_dispatch,
    resolve_skill_dispatch,
)
from ouroboros.router.dispatch import resolve_packaged_skill_path
from ouroboros.router.types import (
    InvalidSkill as TypesInvalidSkill,
)
from ouroboros.router.types import (
    NotHandled as TypesNotHandled,
)
from ouroboros.router.types import (
    Resolved as TypesResolved,
)
from ouroboros.router.types import (
    ResolveResult as TypesResolveResult,
)

_ROUTER_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "router"
_FRONTMATTER_BODY_SKILLS_DIR = _ROUTER_FIXTURES_DIR / "skills" / "frontmatter-body"


def test_load_skill_frontmatter_reads_valid_yaml_mapping(tmp_path: Path) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text(
        """---
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  nested:
    cwd: "$CWD"
---
# Run
Body text with --- in the markdown should not affect frontmatter parsing.
""",
        encoding="utf-8",
    )

    assert load_skill_frontmatter(skill_md_path) == {
        "name": "run",
        "mcp_tool": "ouroboros_execute_seed",
        "mcp_args": {
            "seed_path": "$1",
            "nested": {"cwd": "$CWD"},
        },
    }


def test_load_skill_frontmatter_reads_fixture_metadata_without_body_content() -> None:
    skill_md_path = _FRONTMATTER_BODY_SKILLS_DIR / "run" / "SKILL.md"
    raw_content = skill_md_path.read_text(encoding="utf-8")

    frontmatter = load_skill_frontmatter(skill_md_path)

    assert "body_should_not_be_loaded" in raw_content
    assert "body-alias" in raw_content
    assert frontmatter == {
        "name": "run",
        "aliases": ["execute"],
        "mcp_tool": "ouroboros_execute_seed",
        "mcp_args": {
            "seed_path": "$1",
            "cwd": "$CWD",
            "summary": "seed=$1 cwd=$CWD",
        },
    }
    assert "body_should_not_be_loaded" not in str(frontmatter)
    assert "body-alias" not in str(frontmatter)


def test_router_resolves_fixture_from_frontmatter_not_markdown_body(
    tmp_path: Path,
) -> None:
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo execute seeds/alpha.yaml",
            cwd=runtime_cwd,
            skills_dir=_FRONTMATTER_BODY_SKILLS_DIR,
        )
    )
    body_alias_result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo body-alias seeds/alpha.yaml",
            cwd=runtime_cwd,
            skills_dir=_FRONTMATTER_BODY_SKILLS_DIR,
        )
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == "ooo execute"
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {
        "seed_path": "seeds/alpha.yaml",
        "cwd": str(runtime_cwd),
        "summary": f"seed=seeds/alpha.yaml cwd={runtime_cwd}",
    }
    assert isinstance(body_alias_result, NotHandled)
    assert body_alias_result.category is NoMatchReason.SKILL_NOT_FOUND


@pytest.mark.parametrize(
    "contents",
    [
        "# Run\n",
        "---\n---\n# Run\n",
        "---\n# comment only\n---\n# Run\n",
    ],
)
def test_load_skill_frontmatter_returns_empty_mapping_when_absent_or_empty(
    tmp_path: Path,
    contents: str,
) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text(contents, encoding="utf-8")

    assert load_skill_frontmatter(skill_md_path) == {}


def test_load_skill_frontmatter_rejects_unterminated_frontmatter(tmp_path: Path) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text("---\nname: run\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unterminated frontmatter"):
        load_skill_frontmatter(skill_md_path)


def test_load_skill_frontmatter_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text("---\n- run\n---\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter must be a mapping"):
        load_skill_frontmatter(skill_md_path)


def test_load_skill_frontmatter_preserves_yaml_parser_errors(tmp_path: Path) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text("---\nmcp_args: [\n---\n", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_skill_frontmatter(skill_md_path)


@pytest.mark.parametrize(
    ("frontmatter_text", "expected_tool", "expected_args"),
    [
        pytest.param(
            """\
name: status
description: "Status with block mapping args"
mcp_tool: ouroboros_session_status
mcp_args:
  session_id: "$1"
""",
            "ouroboros_session_status",
            {"session_id": "$1"},
            id="block-mapping-single-template",
        ),
        pytest.param(
            """\
name: run
mcp_tool: " ouroboros_execute_seed "
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
  model_tier: medium
  max_iterations: 10
  skip_qa: false
""",
            "ouroboros_execute_seed",
            {
                "seed_path": "$1",
                "cwd": "$CWD",
                "model_tier": "medium",
                "max_iterations": 10,
                "skip_qa": False,
            },
            id="trimmed-tool-mixed-scalars",
        ),
        pytest.param(
            """\
mcp_tool: ouroboros_evaluate
mcp_args: {artifact: "$1", trigger_consensus: true, acceptance_criteria: ["AC1", "AC2"]}
""",
            "ouroboros_evaluate",
            {
                "artifact": "$1",
                "trigger_consensus": True,
                "acceptance_criteria": ["AC1", "AC2"],
            },
            id="flow-style-map-and-sequence",
        ),
        pytest.param(
            """\
mcp_tool: ouroboros_pm_interview
mcp_args:
  selected_repos:
    - core
    - client
  metadata:
    retry_count: 0
    confidence: 0.75
    note:
""",
            "ouroboros_pm_interview",
            {
                "selected_repos": ["core", "client"],
                "metadata": {
                    "retry_count": 0,
                    "confidence": 0.75,
                    "note": None,
                },
            },
            id="nested-containers-null-and-float",
        ),
        pytest.param(
            """\
mcp_tool: ouroboros_brownfield
mcp_args: {}
""",
            "ouroboros_brownfield",
            {},
            id="empty-args-map",
        ),
    ],
)
def test_valid_mcp_frontmatter_variants_normalize_to_canonical_output(
    tmp_path: Path,
    frontmatter_text: str,
    expected_tool: str,
    expected_args: dict[str, Any],
) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter_text}---\n# Skill\n", encoding="utf-8")

    frontmatter = load_skill_frontmatter(skill_md_path)
    normalized, error = normalize_mcp_frontmatter(frontmatter)

    assert error is None
    assert isinstance(normalized, NormalizedMCPFrontmatter)
    assert normalized == NormalizedMCPFrontmatter(
        mcp_tool=expected_tool,
        mcp_args=expected_args,
    )
    assert type(normalized.mcp_args) is dict


def test_default_mcp_frontmatter_normalization_uses_expected_canonical_defaults() -> None:
    normalized, error = normalize_mcp_frontmatter(
        {
            "mcp_tool": " ouroboros_help ",
            "mcp_args": {},
        }
    )

    assert error is None
    assert normalized == NormalizedMCPFrontmatter(
        mcp_tool="ouroboros_help",
        mcp_args={},
    )
    assert normalized.target == MCPDispatchTarget(
        mcp_tool="ouroboros_help",
        mcp_args={},
    )
    assert normalized.target.kind is DispatchTargetKind.MCP_TOOL


def test_router_resolves_minimal_frontmatter_with_optional_metadata_omitted(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "status"
    skill_dir.mkdir()
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        """---
mcp_tool: ouroboros_session_status
mcp_args: {}
---
# Status
""",
        encoding="utf-8",
    )

    result = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo status", cwd=tmp_path, skills_dir=tmp_path)
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "status"
    assert result.command_prefix == "ooo status"
    assert result.prompt == "ooo status"
    assert result.skill_path == skill_md_path
    assert result.first_argument is None
    assert result.mcp_tool == "ouroboros_session_status"
    assert result.mcp_args == {}
    assert result.dispatch_metadata == NormalizedMCPFrontmatter(
        mcp_tool="ouroboros_session_status",
        mcp_args={},
    )
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_session_status",
        mcp_args={},
    )
    assert result.target.kind is DispatchTargetKind.MCP_TOOL
    assert result.outcome is ResolveOutcome.MATCH


def test_resolve_packaged_skill_path_wraps_codex_artifact_resolver(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        "---\nname: run\nmcp_tool: ouroboros_execute_seed\nmcp_args: {}\n---\n",
        encoding="utf-8",
    )

    with patch(
        "ouroboros.router.dispatch.resolve_packaged_codex_skill_path",
        wraps=__import__(
            "ouroboros.codex",
            fromlist=["resolve_packaged_codex_skill_path"],
        ).resolve_packaged_codex_skill_path,
    ) as resolver:
        with resolve_packaged_skill_path("run", skills_dir=skills_dir) as resolved:
            assert resolved == skill_md_path

    resolver.assert_called_once_with("run", skills_dir=skills_dir)


def test_resolve_packaged_skill_path_keeps_packaged_resource_context_open(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "ephemeral-skills"
    skill_md_path = skills_root / "run" / "SKILL.md"

    @contextmanager
    def ephemeral_resolver(skill_name: str, *, skills_dir: str | Path | None = None):
        assert skill_name == "run"
        assert skills_dir is None
        skill_md_path.parent.mkdir(parents=True)
        skill_md_path.write_text(
            "---\nname: run\nmcp_tool: ouroboros_execute_seed\nmcp_args: {}\n---\n",
            encoding="utf-8",
        )
        try:
            yield skill_md_path
        finally:
            shutil.rmtree(skills_root)

    with patch(
        "ouroboros.router.dispatch.resolve_packaged_codex_skill_path",
        ephemeral_resolver,
    ):
        with resolve_packaged_skill_path("run") as resolved:
            assert resolved != skill_md_path
            assert resolved.is_file()
            assert resolved.read_text(encoding="utf-8") == skill_md_path.read_text(encoding="utf-8")
            cached_path = resolved

    assert not skill_md_path.exists()
    assert cached_path.is_file()


def test_router_resolve_loads_frontmatter_from_registry_target_path(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        '---\nname: run\nmcp_tool: ouroboros_execute_seed\nmcp_args:\n  seed_path: "$1"\n---\n',
        encoding="utf-8",
    )

    with patch(
        "ouroboros.router.dispatch.resolve_packaged_codex_skill_path",
        wraps=__import__(
            "ouroboros.codex",
            fromlist=["resolve_packaged_codex_skill_path"],
        ).resolve_packaged_codex_skill_path,
    ) as resolver:
        result = SkillDispatchRouter().resolve("ooo run seed.yaml", skills_dir=skills_dir)

    assert isinstance(result, Resolved)
    assert result.skill_path == skill_md_path
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {"seed_path": "seed.yaml"}
    resolver.assert_not_called()


def test_router_result_keeps_packaged_skill_path_readable_after_resource_contexts_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_skills_root = tmp_path / "registry-skills"
    resolver_skills_root = tmp_path / "resolver-skills"
    registry_skill_md_path = registry_skills_root / "run" / "SKILL.md"
    resolver_skill_md_path = resolver_skills_root / "run" / "SKILL.md"
    skill_content = (
        '---\nname: run\nmcp_tool: ouroboros_execute_seed\nmcp_args:\n  seed_path: "$1"\n---\n'
    )

    @contextmanager
    def ephemeral_registry_skills(
        *,
        skills_dir: str | Path | None = None,
        anchor_file: str | Path,
        package: str = "ouroboros.skills",
    ):
        assert skills_dir is None
        assert anchor_file
        assert package == "ouroboros.skills"
        registry_skill_md_path.parent.mkdir(parents=True)
        registry_skill_md_path.write_text(skill_content, encoding="utf-8")
        try:
            yield registry_skills_root
        finally:
            shutil.rmtree(registry_skills_root)

    @contextmanager
    def ephemeral_skill_resolver(skill_name: str, *, skills_dir: str | Path | None = None):
        assert skill_name == "run"
        assert skills_dir is None
        resolver_skill_md_path.parent.mkdir(parents=True)
        resolver_skill_md_path.write_text(skill_content, encoding="utf-8")
        try:
            yield resolver_skill_md_path
        finally:
            shutil.rmtree(resolver_skills_root)

    monkeypatch.setattr(
        "ouroboros.router.registry.resolve_packaged_skills_dir",
        ephemeral_registry_skills,
    )

    with patch(
        "ouroboros.router.dispatch.resolve_packaged_codex_skill_path",
        ephemeral_skill_resolver,
    ):
        result = SkillDispatchRouter().resolve("ooo run seed.yaml")

    assert isinstance(result, Resolved)
    assert result.skill_path != resolver_skill_md_path
    assert result.skill_path.is_file()
    assert result.skill_path.read_text(encoding="utf-8") == skill_content
    assert not registry_skill_md_path.exists()
    assert not resolver_skill_md_path.exists()


def test_public_router_parses_raw_ooo_input_and_extracts_argument(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        "---\n"
        "name: run\n"
        "mcp_tool: ouroboros_execute_seed\n"
        "mcp_args:\n"
        '  seed_path: "$1"\n'
        '  cwd: "$CWD"\n'
        '  summary: "cwd=$CWD seed=$1"\n'
        "---\n",
        encoding="utf-8",
    )
    runtime_cwd = tmp_path / "workspace"
    prompt = ' \tOoO   Run\t"seed file.yaml" --max-iterations 2'

    result = resolve_skill_dispatch(prompt, cwd=runtime_cwd, skills_dir=skills_dir)

    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == "ooo run"
    assert result.prompt == prompt
    assert result.skill_path == skill_md_path
    assert result.first_argument == "seed file.yaml --max-iterations 2"
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {
        "seed_path": "seed file.yaml --max-iterations 2",
        "cwd": str(runtime_cwd),
        "summary": f"cwd={runtime_cwd} seed=seed file.yaml --max-iterations 2",
    }


def test_packaged_auto_resolves_documented_chat_dispatch_flags(tmp_path: Path) -> None:
    runtime_cwd = tmp_path / "workspace"
    runtime_cwd.mkdir()
    prompt = (
        'ooo auto "Build a hello CLI" --complete-product '
        "--pipeline-timeout-seconds 600.5 --max-interview-rounds 3 --skip-run"
    )

    result = resolve_skill_dispatch(ResolveRequest(prompt=prompt, cwd=runtime_cwd))

    assert isinstance(result, Resolved)
    assert result.skill_name == "auto"
    assert result.mcp_tool == "ouroboros_auto"
    assert result.mcp_args["goal"] == "Build a hello CLI"
    assert result.mcp_args["cwd"] == str(runtime_cwd)
    assert result.mcp_args["complete_product"] is True
    assert result.mcp_args["pipeline_timeout_seconds"] == 600.5
    assert isinstance(result.mcp_args["pipeline_timeout_seconds"], float)
    assert result.mcp_args["max_interview_rounds"] == 3
    assert result.mcp_args["skip_run"] is True
    assert result.mcp_args["user_preferences"] == ""


def test_packaged_auto_absent_new_flags_default_false_compatible(tmp_path: Path) -> None:
    result = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo auto Build a hello CLI", cwd=tmp_path)
    )

    assert isinstance(result, Resolved)
    assert result.mcp_args["goal"] == "Build a hello CLI"
    assert result.mcp_args["complete_product"] == ""
    assert result.mcp_args["pipeline_timeout_seconds"] == ""
    assert result.mcp_args["skip_run"] == ""


def test_packaged_auto_preserves_unknown_flags_and_literal_control_text(
    tmp_path: Path,
) -> None:
    prompt = (
        "ooo auto Build docs mentioning --complete-product and "
        "--pipeline-timeout-seconds 600.5 --unknown flag"
    )

    result = resolve_skill_dispatch(ResolveRequest(prompt=prompt, cwd=tmp_path))

    assert isinstance(result, Resolved)
    assert result.mcp_args["goal"] == (
        "Build docs mentioning --complete-product and "
        "--pipeline-timeout-seconds 600.5 --unknown flag"
    )
    assert result.mcp_args["complete_product"] == ""
    assert result.mcp_args["pipeline_timeout_seconds"] == ""


def test_router_exports_runtime_request_and_result_types(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        "---\n"
        "name: run\n"
        "mcp_tool: ouroboros_execute_seed\n"
        "mcp_args:\n"
        '  seed_path: "$1"\n'
        '  cwd: "$CWD"\n'
        "---\n",
        encoding="utf-8",
    )

    assert RouterRequest is ResolveRequest
    assert ResolveResult is TypesResolveResult
    assert Resolved is TypesResolved
    assert NotHandled is TypesNotHandled
    assert InvalidSkill is TypesInvalidSkill
    assert DispatchTarget is MCPDispatchTarget
    assert McpDispatchTarget is MCPDispatchTarget
    assert TypesResolveResult.__value__ == (TypesResolved | TypesNotHandled | TypesInvalidSkill)

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run seed.yaml",
            cwd=tmp_path,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == "ooo run"
    assert result.prompt == "ooo run seed.yaml"
    assert result.skill_path == skill_md_path
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {"seed_path": "seed.yaml", "cwd": str(tmp_path)}
    assert result.outcome is ResolveOutcome.MATCH
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args={"seed_path": "seed.yaml", "cwd": str(tmp_path)},
    )
    assert result.target.kind is DispatchTargetKind.MCP_TOOL
    assert result.dispatch_target == result.target


def test_resolve_parsed_skill_dispatch_returns_canonical_target_for_known_command(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        "---\n"
        "name: execute\n"
        "aliases:\n"
        "  - start\n"
        "mcp_tool: ouroboros_execute_seed\n"
        "mcp_args:\n"
        '  seed_path: "$1"\n'
        '  cwd: "$CWD"\n'
        "---\n",
        encoding="utf-8",
    )
    runtime_cwd = tmp_path / "workspace"
    parsed = ParsedOooCommand(
        skill_name="start",
        command_prefix="ooo start",
        remainder='"seed file.yaml" --max-iterations 2',
    )

    result = resolve_parsed_skill_dispatch(
        parsed,
        prompt='ooo start "seed file.yaml" --max-iterations 2',
        cwd=runtime_cwd,
        skills_dir=skills_dir,
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "run"
    assert result.command_prefix == "ooo start"
    assert result.prompt == 'ooo start "seed file.yaml" --max-iterations 2'
    assert result.skill_path == skill_md_path
    assert result.first_argument == "seed file.yaml --max-iterations 2"
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args={
            "seed_path": "seed file.yaml --max-iterations 2",
            "cwd": str(runtime_cwd),
        },
    )

    direct_result = resolve_skill_dispatch(parsed, cwd=runtime_cwd, skills_dir=skills_dir)
    assert isinstance(direct_result, Resolved)
    assert direct_result.prompt == 'ooo start "seed file.yaml" --max-iterations 2'
    assert direct_result.target == result.target


def test_resolve_parsed_skill_dispatch_reports_malformed_command_as_invalid_input(
    tmp_path: Path,
) -> None:
    parsed = ParsedOooCommand(
        skill_name="run!",
        command_prefix="ooo run!",
        remainder="seed.yaml",
    )

    result = resolve_parsed_skill_dispatch(parsed, cwd=tmp_path, skills_dir=tmp_path)

    assert isinstance(result, InvalidSkill)
    assert result.reason == (
        "malformed parsed command: skill_name must be a valid skill identifier"
    )
    assert result.category is InvalidInputReason.MALFORMED_PARSED_COMMAND
    assert result.outcome is ResolveOutcome.INVALID_INPUT


def test_resolve_parsed_skill_dispatch_reports_unregistered_valid_command_as_no_match(
    tmp_path: Path,
) -> None:
    parsed = ParsedOooCommand(
        skill_name="missing",
        command_prefix="ooo missing",
        remainder="seed.yaml",
    )

    result = resolve_parsed_skill_dispatch(parsed, cwd=tmp_path, skills_dir=tmp_path)

    assert isinstance(result, NotHandled)
    assert result.reason == "skill not found"
    assert result.category is NoMatchReason.SKILL_NOT_FOUND
    assert result.outcome is ResolveOutcome.NO_MATCH


def test_router_resolves_first_positional_argument_template(tmp_path: Path) -> None:
    skill_dir = tmp_path / "evaluate"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text(
        '---\nname: evaluate\nmcp_tool: ouroboros_evaluate\nmcp_args:\n  artifact: "$1"\n---\n',
        encoding="utf-8",
    )

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt='ooo evaluate "reports/final output.md" --strict',
            cwd=tmp_path,
            skills_dir=tmp_path,
        )
    )

    assert isinstance(result, Resolved)
    assert result.mcp_args == {"artifact": "reports/final output.md --strict"}


def test_router_resolves_working_directory_template_from_request_cwd(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "status"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        '---\nname: status\nmcp_tool: ouroboros_session_status\nmcp_args:\n  cwd: "$CWD"\n---\n',
        encoding="utf-8",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo status",
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.mcp_args == {"cwd": str(runtime_cwd)}


def test_router_resolves_combined_scalar_template_with_multiple_substitutions(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: run\n"
        "mcp_tool: ouroboros_execute_seed\n"
        "mcp_args:\n"
        '  combined: "cwd=$CWD seed=$1 path=$CWD/$1 again=$1"\n'
        "---\n",
        encoding="utf-8",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt='ooo run "seeds/final seed.yaml"',
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.mcp_args == {
        "combined": (
            f"cwd={runtime_cwd} seed=seeds/final seed.yaml "
            f"path={runtime_cwd}/seeds/final seed.yaml again=seeds/final seed.yaml"
        )
    }


def test_router_resolves_templates_recursively_inside_nested_arrays_and_objects(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "evaluate"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: evaluate\n"
        "mcp_tool: ouroboros_evaluate\n"
        "mcp_args:\n"
        '  artifact: "$1"\n'
        "  payload:\n"
        '    cwd: "$CWD"\n'
        "    batches:\n"
        "      - name: primary\n"
        "        files:\n"
        '          - "$1"\n'
        '          - "$CWD/$1"\n'
        "      - nested:\n"
        "          criteria:\n"
        '            - "check $1"\n'
        '            - path: "$CWD/reports"\n'
        "              values:\n"
        '                - "$1"\n'
        '                - "$CWD"\n'
        "---\n",
        encoding="utf-8",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt='ooo evaluate "reports/final output.md"',
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.mcp_args == {
        "artifact": "reports/final output.md",
        "payload": {
            "cwd": str(runtime_cwd),
            "batches": [
                {
                    "name": "primary",
                    "files": [
                        "reports/final output.md",
                        f"{runtime_cwd}/reports/final output.md",
                    ],
                },
                {
                    "nested": {
                        "criteria": [
                            "check reports/final output.md",
                            {
                                "path": f"{runtime_cwd}/reports",
                                "values": [
                                    "reports/final output.md",
                                    str(runtime_cwd),
                                ],
                            },
                        ]
                    }
                },
            ],
        },
    }


def test_router_resolves_only_explicit_template_locations_from_frontmatter(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: run\n"
        "mcp_tool: ouroboros_execute_seed\n"
        "mcp_args:\n"
        "  seed_path: pinned/default-seed.yaml\n"
        "  cwd: pinned/workspace\n"
        '  selected_seed: "$1"\n'
        '  selected_cwd: "$CWD"\n'
        '  combined: "seed=$1 cwd=$CWD"\n'
        "  nested:\n"
        "    literal_seed_path: pinned/nested-seed.yaml\n"
        '    selected_seed_path: "$CWD/$1"\n'
        "---\n",
        encoding="utf-8",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run seeds/selected.yaml",
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.mcp_args == {
        "seed_path": "pinned/default-seed.yaml",
        "cwd": "pinned/workspace",
        "selected_seed": "seeds/selected.yaml",
        "selected_cwd": str(runtime_cwd),
        "combined": f"seed=seeds/selected.yaml cwd={runtime_cwd}",
        "nested": {
            "literal_seed_path": "pinned/nested-seed.yaml",
            "selected_seed_path": f"{runtime_cwd}/seeds/selected.yaml",
        },
    }


def test_resolve_dispatch_templates_substitutes_inside_string_scalars(
    tmp_path: Path,
) -> None:
    result = resolve_dispatch_templates(
        {
            "seed_path": "$CWD/$1",
            "label": "seed=$1 cwd=$CWD",
            "repeat": "$1:$1",
            "values": ["$CWD/project", 7, True, None],
        },
        first_argument="seed.yaml",
        cwd=tmp_path,
    )

    assert result == {
        "seed_path": f"{tmp_path}/seed.yaml",
        "label": f"seed=seed.yaml cwd={tmp_path}",
        "repeat": "seed.yaml:seed.yaml",
        "values": [f"{tmp_path}/project", 7, True, None],
    }


def test_resolve_dispatch_templates_replaces_missing_first_argument_with_empty_string(
    tmp_path: Path,
) -> None:
    result = resolve_dispatch_templates(
        {
            "exact": "$1",
            "path": "$CWD/$1",
            "label": "seed=$1 cwd=$CWD",
            "nested": ["$1", {"again": "$1"}],
        },
        first_argument=None,
        cwd=tmp_path,
    )

    assert result == {
        "exact": "",
        "path": f"{tmp_path}/",
        "label": f"seed= cwd={tmp_path}",
        "nested": ["", {"again": ""}],
    }


def test_resolve_dispatch_templates_preserves_null_results_without_stringifying(
    tmp_path: Path,
) -> None:
    assert resolve_dispatch_templates(None, first_argument="seed.yaml", cwd=tmp_path) is None

    result = resolve_dispatch_templates(
        {
            "optional": None,
            "nested": [None, {"fallback": None, "template": "$1"}],
        },
        first_argument=None,
        cwd=tmp_path,
    )

    assert result == {
        "optional": None,
        "nested": [None, {"fallback": None, "template": ""}],
    }


def test_resolve_dispatch_templates_recurses_only_into_string_leaves(
    tmp_path: Path,
) -> None:
    result = resolve_dispatch_templates(
        {
            "$CWD": "$CWD",
            "matrix": [
                [
                    "$1",
                    {
                        "path": "$CWD/$1",
                        "flags": [False, 0, 1.25, None],
                    },
                ],
                {
                    "items": [
                        {"literal": "before $1 after"},
                        ["$CWD", {"count": 3}],
                    ],
                },
            ],
            "enabled": True,
        },
        first_argument="seed.yaml",
        cwd=tmp_path,
    )

    assert result == {
        "$CWD": str(tmp_path),
        "matrix": [
            [
                "seed.yaml",
                {
                    "path": f"{tmp_path}/seed.yaml",
                    "flags": [False, 0, 1.25, None],
                },
            ],
            {
                "items": [
                    {"literal": "before seed.yaml after"},
                    [str(tmp_path), {"count": 3}],
                ],
            },
        ],
        "enabled": True,
    }


def test_resolve_dispatch_templates_does_not_reprocess_replacement_text(
    tmp_path: Path,
) -> None:
    result = resolve_dispatch_templates(
        "$1/$CWD",
        first_argument="$CWD",
        cwd=tmp_path,
    )

    assert result == f"$CWD/{tmp_path}"


def test_resolve_dispatch_templates_selects_only_exact_supported_placeholders(
    tmp_path: Path,
) -> None:
    result = resolve_dispatch_templates(
        {
            "selected": ["$1", "$CWD", "seed=$1 cwd=$CWD", "$CWD/$1"],
            "lookalikes": [
                "$10",
                "$01",
                "$1_suffix",
                "${1}",
                "$CWD_SUFFIX",
                "${CWD}",
                "$cwd",
                "$2",
            ],
        },
        first_argument="seed.yaml",
        cwd=tmp_path,
    )

    assert result == {
        "selected": [
            "seed.yaml",
            str(tmp_path),
            f"seed=seed.yaml cwd={tmp_path}",
            f"{tmp_path}/seed.yaml",
        ],
        "lookalikes": [
            "$10",
            "$01",
            "$1_suffix",
            "${1}",
            "$CWD_SUFFIX",
            "${CWD}",
            "$cwd",
            "$2",
        ],
    }


def test_resolve_dispatch_templates_falls_back_to_literal_values_without_exact_match(
    tmp_path: Path,
) -> None:
    result = resolve_dispatch_templates(
        {
            "seed_path": "$1_default",
            "cwd": "$CWD_DEFAULT",
            "message": "keep ${1} and ${CWD} literal",
            "nested": [
                "$10",
                "$CWD2",
                {
                    "description": "unsupported $2 placeholder",
                    "$1": "mapping keys are not templates",
                },
            ],
        },
        first_argument="seed.yaml",
        cwd=tmp_path,
    )

    assert result == {
        "seed_path": "$1_default",
        "cwd": "$CWD_DEFAULT",
        "message": "keep ${1} and ${CWD} literal",
        "nested": [
            "$10",
            "$CWD2",
            {
                "description": "unsupported $2 placeholder",
                "$1": "mapping keys are not templates",
            },
        ],
    }


def test_normalize_mcp_frontmatter_returns_public_canonical_shape() -> None:
    normalized, error = normalize_mcp_frontmatter(
        {
            "mcp_tool": " ouroboros_execute_seed ",
            "mcp_args": {
                "seed_path": "$1",
                "nested": {
                    "values": ["$CWD", 7, True, None],
                },
            },
        }
    )

    assert error is None
    assert isinstance(normalized, NormalizedMCPFrontmatter)
    assert NormalizedMcpFrontmatter is NormalizedMCPFrontmatter
    assert normalized.mcp_tool == "ouroboros_execute_seed"
    assert normalized.mcp_args == {
        "seed_path": "$1",
        "nested": {
            "values": ["$CWD", 7, True, None],
        },
    }
    assert normalized.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args={
            "seed_path": "$1",
            "nested": {
                "values": ["$CWD", 7, True, None],
            },
        },
    )
    assert tuple(normalized) == (normalized.mcp_tool, normalized.mcp_args)


@pytest.mark.parametrize(
    ("frontmatter", "expected_error"),
    [
        pytest.param(
            {},
            "missing required frontmatter key: mcp_tool",
            id="missing-all-required-keys",
        ),
        pytest.param(
            {"mcp_args": {}},
            "missing required frontmatter key: mcp_tool",
            id="missing-tool-key",
        ),
        pytest.param(
            {"mcp_tool": "ouroboros_help"},
            "missing required frontmatter key: mcp_args",
            id="missing-args-key",
        ),
        pytest.param(
            {"mcp_tool": None, "mcp_args": {}},
            "mcp_tool must be a non-empty string",
            id="tool-null-is-present-but-invalid",
        ),
        pytest.param(
            {"mcp_tool": "   ", "mcp_args": {}},
            "mcp_tool must be a non-empty string",
            id="tool-blank-is-present-but-invalid",
        ),
        pytest.param(
            {"mcp_tool": "ouroboros_help", "mcp_args": None},
            "mcp_args must be a mapping with string keys and YAML-safe values",
            id="args-null-is-present-but-invalid",
        ),
        pytest.param(
            [],
            "SKILL.md frontmatter must be a mapping",
            id="frontmatter-not-mapping",
        ),
    ],
)
def test_normalize_mcp_frontmatter_reports_required_key_and_shape_errors(
    frontmatter: Any,
    expected_error: str,
) -> None:
    normalized, error = normalize_mcp_frontmatter(frontmatter)

    assert normalized is None
    assert error == expected_error


@pytest.mark.parametrize(
    ("mcp_args", "expected_error"),
    [
        pytest.param(
            {"seed_path": ("$1", "$CWD")},
            (
                "mcp_args.seed_path has unsupported type tuple; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="tuple-sequence",
        ),
        pytest.param(
            {"created_at": date(2026, 4, 20)},
            (
                "mcp_args.created_at has unsupported type date; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="date-scalar",
        ),
        pytest.param(
            {"payload": b"binary"},
            (
                "mcp_args.payload has unsupported type bytes; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="bytes-scalar",
        ),
        pytest.param(
            {"labels": {"a", "b"}},
            (
                "mcp_args.labels has unsupported type set; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="set-container",
        ),
        pytest.param(
            {"score": float("nan")},
            "mcp_args.score must be a finite number",
            id="nan-float",
        ),
        pytest.param(
            {"score": float("inf")},
            "mcp_args.score must be a finite number",
            id="infinite-float",
        ),
        pytest.param(
            {"metadata": {1: "numeric-key"}},
            "mcp_args.metadata keys must be non-empty strings",
            id="nested-non-string-key",
        ),
        pytest.param(
            {"metadata": {" ": "blank-key"}},
            "mcp_args.metadata keys must be non-empty strings",
            id="nested-blank-key",
        ),
    ],
)
def test_normalize_mcp_frontmatter_rejects_incompatible_mcp_value_types(
    mcp_args: dict[Any, Any],
    expected_error: str,
) -> None:
    normalized, error = normalize_mcp_frontmatter(
        {
            "mcp_tool": "ouroboros_execute_seed",
            "mcp_args": mcp_args,
        }
    )

    assert normalized is None
    assert error == expected_error


def test_normalize_mcp_frontmatter_rejects_yaml_implicit_non_json_values(
    tmp_path: Path,
) -> None:
    skill_md_path = tmp_path / "SKILL.md"
    skill_md_path.write_text(
        """---
mcp_tool: ouroboros_execute_seed
mcp_args:
  created_at: 2026-04-20
---
""",
        encoding="utf-8",
    )

    frontmatter = load_skill_frontmatter(skill_md_path)
    normalized, error = normalize_mcp_frontmatter(frontmatter)

    assert normalized is None
    assert error == (
        "mcp_args.created_at has unsupported type date; "
        "expected string, finite number, boolean, null, list, or mapping"
    )


def test_normalize_mcp_frontmatter_clones_plain_canonical_containers() -> None:
    raw_nested = OrderedDict([("seed_path", "$1")])
    raw_args = OrderedDict(
        [
            ("items", [raw_nested, "$CWD"]),
            ("metadata", OrderedDict([("enabled", True)])),
        ]
    )

    normalized, error = normalize_mcp_frontmatter(
        {
            "mcp_tool": " ouroboros_execute_seed ",
            "mcp_args": raw_args,
        }
    )

    assert error is None
    assert isinstance(normalized, NormalizedMCPFrontmatter)
    assert normalized.mcp_tool == "ouroboros_execute_seed"
    assert normalized.mcp_args == {
        "items": [{"seed_path": "$1"}, "$CWD"],
        "metadata": {"enabled": True},
    }
    assert type(normalized.mcp_args) is dict
    assert type(normalized.mcp_args["items"]) is list
    assert type(normalized.mcp_args["items"][0]) is dict
    assert type(normalized.mcp_args["metadata"]) is dict

    raw_nested["seed_path"] = "changed.yaml"
    assert normalized.mcp_args["items"][0]["seed_path"] == "$1"


def test_router_result_types_cover_not_handled_and_invalid_skill(tmp_path: Path) -> None:
    not_handled = resolve_skill_dispatch(
        ResolveRequest(prompt="please run seed.yaml", cwd=tmp_path, skills_dir=tmp_path)
    )

    assert isinstance(not_handled, NotHandled)
    assert not_handled.reason == "not a skill command"
    assert not_handled.category is NoMatchReason.NOT_A_SKILL_COMMAND
    assert not_handled.outcome is ResolveOutcome.NO_MATCH

    missing = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo missing", cwd=tmp_path, skills_dir=tmp_path)
    )

    assert isinstance(missing, NotHandled)
    assert missing.reason == "skill not found"
    assert missing.category is NoMatchReason.SKILL_NOT_FOUND
    assert missing.outcome is ResolveOutcome.NO_MATCH

    skill_dir = tmp_path / "help"
    skill_dir.mkdir()
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text("---\nname: help\n---\n", encoding="utf-8")

    invalid = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo help", cwd=tmp_path, skills_dir=tmp_path)
    )

    assert isinstance(invalid, InvalidSkill)
    assert invalid.reason == "missing required frontmatter key: mcp_tool"
    assert invalid.skill_path == skill_md_path
    assert invalid.category is InvalidInputReason.FRONTMATTER_INVALID
    assert invalid.outcome is ResolveOutcome.INVALID_INPUT


def test_public_router_prompt_string_non_dispatch_returns_typed_not_handled(
    tmp_path: Path,
) -> None:
    result = resolve_skill_dispatch(
        "please run seed.yaml",
        cwd=tmp_path,
        skills_dir=tmp_path,
    )

    assert isinstance(result, NotHandled)
    assert result.reason == "not a skill command"
    assert result.category is NoMatchReason.NOT_A_SKILL_COMMAND
    assert result.outcome is ResolveOutcome.NO_MATCH


@pytest.mark.parametrize(
    ("frontmatter_text", "expected_error"),
    [
        pytest.param(
            "name: run\n",
            "missing required frontmatter key: mcp_tool",
            id="missing-all-required-keys",
        ),
        pytest.param(
            "name: run\nmcp_args: {}\n",
            "missing required frontmatter key: mcp_tool",
            id="missing-tool-key",
        ),
        pytest.param(
            "name: run\nmcp_tool: ouroboros_execute_seed\n",
            "missing required frontmatter key: mcp_args",
            id="missing-args-key",
        ),
    ],
)
def test_router_reports_missing_required_frontmatter_keys_as_invalid_skill(
    tmp_path: Path,
    frontmatter_text: str,
    expected_error: str,
) -> None:
    skill_dir = tmp_path / "run"
    skill_dir.mkdir()
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter_text}---\n# Run\n", encoding="utf-8")

    invalid = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo run seed.yaml", cwd=tmp_path, skills_dir=tmp_path)
    )

    assert isinstance(invalid, InvalidSkill)
    assert invalid.reason == expected_error
    assert invalid.skill_path == skill_md_path
    assert invalid.category is InvalidInputReason.FRONTMATTER_INVALID
    assert invalid.outcome is ResolveOutcome.INVALID_INPUT


def test_router_reports_malformed_skill_frontmatter_as_invalid_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "run"
    skill_dir.mkdir()
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text("---\nmcp_args: [\n---\n", encoding="utf-8")

    invalid = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo run seed.yaml", cwd=tmp_path, skills_dir=tmp_path)
    )

    assert isinstance(invalid, InvalidSkill)
    assert invalid.skill_path == skill_md_path
    assert invalid.category is InvalidInputReason.FRONTMATTER_LOAD_ERROR
    assert "while parsing" in invalid.reason
