from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.router import (
    MCPDispatchTarget,
    NormalizedMCPFrontmatter,
    ParsedOooCommand,
    Resolved,
    ResolveOutcome,
    ResolveRequest,
    resolve_skill_dispatch,
)


def _write_dispatchable_skill(skills_dir: Path, skill_name: str) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        """---
name: run
mcp_tool: " ouroboros_execute_seed "
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
  combined: "cwd=$CWD seed=$1"
  nested:
    values:
      - "$1"
      - "$CWD"
      - true
---
# Run
""",
        encoding="utf-8",
    )
    return skill_md_path


def _write_alias_dispatchable_skill(skills_dir: Path) -> Path:
    skill_dir = skills_dir / "run"
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        """---
name: execute
alias: Quick-Run
aliases:
  - start
command_aliases: "ooo begin, /ouroboros:launch, dispatch"
skill_aliases:
  - OOO Ship-It
commands:
  - /ouroboros:go
mcp_tool: " ouroboros_execute_seed "
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
  combined: "cwd=$CWD seed=$1"
---
# Run
""",
        encoding="utf-8",
    )
    return skill_md_path


def _assert_resolved_payload(result: object, expected: Resolved) -> None:
    """Assert every canonical Resolved field, including compare=False fields."""
    assert type(result) is Resolved
    resolved = result
    assert resolved.skill_name == expected.skill_name
    assert resolved.command_prefix == expected.command_prefix
    assert resolved.prompt == expected.prompt
    assert resolved.skill_path == expected.skill_path
    assert resolved.mcp_tool == expected.mcp_tool
    assert resolved.mcp_args == expected.mcp_args
    assert resolved.first_argument == expected.first_argument


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            ' \tOoO   Run\t"seed file.yaml" --max-iterations 2',
            "ooo run",
            id="ooo-prefix-normalized",
        ),
        pytest.param(
            ' \t/OUROBOROS:Run   "seed file.yaml" --max-iterations 2',
            "/ouroboros:run",
            id="slash-prefix-normalized",
        ),
    ],
)
def test_valid_dispatch_inputs_normalize_to_canonical_runtime_metadata(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_dispatchable_skill(skills_dir, "run")
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=prompt,
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    expected_argument = "seed file.yaml --max-iterations 2"
    expected_args = {
        "seed_path": expected_argument,
        "cwd": str(runtime_cwd),
        "combined": f"cwd={runtime_cwd} seed={expected_argument}",
        "nested": {
            "values": [
                expected_argument,
                str(runtime_cwd),
                True,
            ],
        },
    }
    _assert_resolved_payload(
        result,
        Resolved(
            skill_name="run",
            command_prefix=expected_prefix,
            prompt=prompt,
            skill_path=skill_md_path,
            mcp_tool="ouroboros_execute_seed",
            mcp_args=expected_args,
            first_argument=expected_argument,
        ),
    )
    assert result.dispatch_metadata == NormalizedMCPFrontmatter(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )
    assert result.dispatch_metadata.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )
    assert result.outcome is ResolveOutcome.MATCH


def test_valid_dispatch_normalizes_trailing_line_ending_on_single_line_argument(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_dispatchable_skill(skills_dir, "run")

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt='ooo run "seed file.yaml"\r\n',
            cwd=tmp_path,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.first_argument == "seed file.yaml"
    assert result.mcp_args["seed_path"] == "seed file.yaml"


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            'ooo run "forms/alpha seed.yaml" --max-iterations 2',
            "ooo run",
            id="direct-skill-ooo-prefix",
        ),
        pytest.param(
            '/ouroboros:run "forms/alpha seed.yaml" --max-iterations 2',
            "/ouroboros:run",
            id="direct-skill-slash-prefix",
        ),
        pytest.param(
            'OoO Execute "forms/alpha seed.yaml" --max-iterations 2',
            "ooo execute",
            id="frontmatter-name",
        ),
        pytest.param(
            'ooo Quick-Run "forms/alpha seed.yaml" --max-iterations 2',
            "ooo quick-run",
            id="single-alias-field",
        ),
        pytest.param(
            'ooo start "forms/alpha seed.yaml" --max-iterations 2',
            "ooo start",
            id="aliases-sequence-field",
        ),
        pytest.param(
            'ooo begin "forms/alpha seed.yaml" --max-iterations 2',
            "ooo begin",
            id="command-alias-ooo-value",
        ),
        pytest.param(
            '/OUROBOROS:Launch "forms/alpha seed.yaml" --max-iterations 2',
            "/ouroboros:launch",
            id="command-alias-slash-value",
        ),
        pytest.param(
            'ooo dispatch "forms/alpha seed.yaml" --max-iterations 2',
            "ooo dispatch",
            id="command-alias-bare-value",
        ),
        pytest.param(
            'OOO Ship-It "forms/alpha seed.yaml" --max-iterations 2',
            "ooo ship-it",
            id="skill-alias-prefixed-value",
        ),
        pytest.param(
            '/ouroboros:go "forms/alpha seed.yaml" --max-iterations 2',
            "/ouroboros:go",
            id="commands-sequence-field",
        ),
    ],
)
def test_valid_router_dispatch_forms_resolve_expected_parsed_dispatch_fields(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_alias_dispatchable_skill(skills_dir)
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=prompt,
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    expected_argument = "forms/alpha seed.yaml --max-iterations 2"
    expected_args = {
        "seed_path": expected_argument,
        "cwd": str(runtime_cwd),
        "combined": f"cwd={runtime_cwd} seed={expected_argument}",
    }
    _assert_resolved_payload(
        result,
        Resolved(
            skill_name="run",
            command_prefix=expected_prefix,
            prompt=prompt,
            skill_path=skill_md_path,
            mcp_tool="ouroboros_execute_seed",
            mcp_args=expected_args,
            first_argument=expected_argument,
        ),
    )
    assert result.dispatch_metadata == NormalizedMCPFrontmatter(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )
    assert result.outcome is ResolveOutcome.MATCH


def test_valid_dispatch_preserves_multiline_inline_seed_payload(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_dispatchable_skill(skills_dir, "run")
    runtime_cwd = tmp_path / "workspace"
    seed_content = "goal: test\nconstraints:\n  - keep it simple\nacceptance_criteria:\n  - works"
    prompt = f"/ouroboros:run\n{seed_content}"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=prompt,
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    expected_args = {
        "seed_path": seed_content,
        "cwd": str(runtime_cwd),
        "combined": f"cwd={runtime_cwd} seed={seed_content}",
        "nested": {
            "values": [
                seed_content,
                str(runtime_cwd),
                True,
            ],
        },
    }
    _assert_resolved_payload(
        result,
        Resolved(
            skill_name="run",
            command_prefix="/ouroboros:run",
            prompt=prompt,
            skill_path=skill_md_path,
            mcp_tool="ouroboros_execute_seed",
            mcp_args=expected_args,
            first_argument=seed_content,
        ),
    )
    assert result.outcome is ResolveOutcome.MATCH


def test_valid_dispatch_preserves_multiline_inline_seed_leading_whitespace(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_dispatchable_skill(skills_dir, "run")
    seed_content = "  goal: test\n  constraints:\n    - keep it simple"
    prompt = f"/ouroboros:run\n{seed_content}"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=prompt,
            cwd=tmp_path,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.first_argument == seed_content
    assert result.mcp_args["seed_path"] == seed_content


def test_valid_parsed_dispatch_reconstructs_multiline_prompt_with_newline_separator(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_dispatchable_skill(skills_dir, "run")
    seed_content = "goal: test\nconstraints:\n  - keep it simple"
    parsed = ParsedOooCommand(
        skill_name="run",
        command_prefix="/ouroboros:run",
        remainder=seed_content,
    )

    result = resolve_skill_dispatch(parsed, cwd=tmp_path, skills_dir=skills_dir)

    assert isinstance(result, Resolved)
    assert result.prompt == f"/ouroboros:run\n{seed_content}"
    assert result.first_argument == seed_content
    assert result.mcp_args["seed_path"] == seed_content


def test_valid_dispatch_without_argument_normalizes_first_argument_template_to_empty_string(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_dispatchable_skill(skills_dir, "run")

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run",
            cwd=tmp_path,
            skills_dir=skills_dir,
        )
    )

    expected_args = {
        "seed_path": "",
        "cwd": str(tmp_path),
        "combined": f"cwd={tmp_path} seed=",
        "nested": {
            "values": [
                "",
                str(tmp_path),
                True,
            ],
        },
    }
    _assert_resolved_payload(
        result,
        Resolved(
            skill_name="run",
            command_prefix="ooo run",
            prompt="ooo run",
            skill_path=skill_md_path,
            mcp_tool="ouroboros_execute_seed",
            mcp_args=expected_args,
            first_argument=None,
        ),
    )
