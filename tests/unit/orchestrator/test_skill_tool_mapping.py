from __future__ import annotations

from pathlib import Path

from ouroboros.orchestrator.skill_tool_mapping import (
    SkillToolMapping,
    _packaged_skills_dir,
    discover_skill_body_context_keys,
    discover_skill_context_keys,
    discover_skill_tool_mappings,
    extract_skill_body_tool_context_keys,
    extract_skill_frontmatter_context_keys,
    merge_skill_context_keys_by_tool,
    merge_tool_context_keys,
    skill_frontmatter_context_keys_by_tool,
)

_CORE_FRONTMATTER_SKILL_TOOLS = {
    "auto": "ouroboros_start_auto",
    "run": "ouroboros_execute_seed",
    "ralph": "ouroboros_ralph",
    "status": "ouroboros_session_status",
    "seed": "ouroboros_generate_seed",
    "interview": "ouroboros_interview",
}


def test_extract_skill_frontmatter_context_keys_preserves_mcp_arg_order() -> None:
    frontmatter = {
        "mcp_tool": "ouroboros_start_auto",
        "mcp_args": {
            "goal": "$goal",
            "resume": "$resume",
            "cwd": "$CWD",
            "max_interview_rounds": "$max_interview_rounds",
        },
    }

    assert extract_skill_frontmatter_context_keys(frontmatter) == (
        "goal",
        "resume",
        "cwd",
        "max_interview_rounds",
    )


def test_extract_skill_frontmatter_context_keys_handles_missing_mcp_args() -> None:
    assert extract_skill_frontmatter_context_keys({"name": "unstuck"}) == ()
    assert extract_skill_frontmatter_context_keys({"mcp_args": ["session_id"]}) == ()


def test_extract_skill_body_tool_context_keys_reads_tool_blocks_and_inline_calls() -> None:
    body = """
## Start

Tool: ouroboros_start_probe
Arguments:
  seed_content: <the seed YAML>
  model_tier: "medium"
  metadata: |
    nested_key: ignored
  max_iterations: 10

Use `ouroboros_job_wait(job_id, cursor, timeout_seconds=120, view="summary")`
and then `ouroboros_job_result(job_id=<job_id>)`.

Tool: ouroboros_start_probe
Arguments:
  session_id: <existing session ID>
  seed_content: <same seed YAML>
"""

    assert extract_skill_body_tool_context_keys(body) == {
        "ouroboros_start_probe": ("seed_content", "metadata", "session_id"),
        "ouroboros_job_wait": ("job_id", "cursor"),
        "ouroboros_job_result": ("job_id",),
    }


def test_extract_skill_body_tool_context_keys_ignores_frontmatter_only_usage() -> None:
    skill_md = """---
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
---

The body has no direct tool invocation.
"""

    assert extract_skill_body_tool_context_keys(skill_md) == {}


def test_packaged_skills_dir_supports_installed_wheel_layout(tmp_path: Path) -> None:
    package_root = tmp_path / "site-packages" / "ouroboros"
    module_file = package_root / "orchestrator" / "skill_tool_mapping.py"
    wheel_skills_dir = package_root / "skills"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")
    wheel_skills_dir.mkdir(parents=True)

    assert _packaged_skills_dir(module_file) == wheel_skills_dir


def test_packaged_skill_frontmatter_exposes_declared_mcp_tools() -> None:
    mappings = discover_skill_tool_mappings()
    by_skill = {mapping.skill_name: mapping for mapping in mappings}

    assert set(_CORE_FRONTMATTER_SKILL_TOOLS) == {
        "auto",
        "run",
        "ralph",
        "status",
        "seed",
        "interview",
    }
    assert {
        skill: by_skill[skill].mcp_tool for skill in _CORE_FRONTMATTER_SKILL_TOOLS
    } == _CORE_FRONTMATTER_SKILL_TOOLS
    assert {skill: by_skill[skill].skill_path for skill in _CORE_FRONTMATTER_SKILL_TOOLS} == {
        skill: f"skills/{skill}/SKILL.md" for skill in _CORE_FRONTMATTER_SKILL_TOOLS
    }


def test_packaged_skill_frontmatter_exposes_tool_specific_context_keys() -> None:
    mappings = discover_skill_tool_mappings()
    by_skill = {mapping.skill_name: mapping for mapping in mappings}

    expected = {
        "auto": (
            "goal",
            "resume",
            "cwd",
            "max_interview_rounds",
            "max_repair_rounds",
            "skip_run",
            "complete_product",
            "pipeline_timeout_seconds",
        ),
        "run": ("seed_path", "cwd"),
        "ralph": ("lineage_id",),
        "status": ("session_id",),
        "seed": ("session_id",),
        "interview": ("initial_context", "cwd"),
    }

    assert {skill: by_skill[skill].context_keys for skill in expected} == expected
    assert skill_frontmatter_context_keys_by_tool(mappings) == {
        by_skill[skill].mcp_tool: context_keys for skill, context_keys in expected.items()
    }


def test_packaged_skill_bodies_expose_tool_specific_context_keys() -> None:
    context_keys_by_tool = discover_skill_body_context_keys()

    expected_subset = {
        "ouroboros_start_execute_seed": ("seed_content", "session_id"),
        "ouroboros_job_wait": ("job_id", "cursor"),
        "ouroboros_job_result": ("job_id",),
        "ouroboros_ac_tree_hud": ("session_id", "cursor"),
        "ouroboros_interview": (
            "initial_context",
            "cwd",
            "session_id",
            "answer",
            "last_question",
        ),
        "ouroboros_lateral_think": (
            "problem_context",
            "failed_attempts",
        ),
        "ouroboros_generate_seed": ("session_id",),
        "ouroboros_session_status": ("session_id",),
        "ouroboros_measure_drift": (
            "session_id",
            "current_output",
            "seed_content",
        ),
    }

    assert {tool: context_keys_by_tool[tool] for tool in expected_subset} == expected_subset


def test_skill_frontmatter_context_keys_by_tool_merges_duplicate_tool_keys() -> None:
    mappings = (
        SkillToolMapping(
            skill_name="first",
            mcp_tool="ouroboros_probe",
            skill_path="skills/first/SKILL.md",
            mcp_args={"session_id": "$1", "cwd": "$CWD"},
            context_keys=("session_id", "cwd"),
        ),
        SkillToolMapping(
            skill_name="second",
            mcp_tool="ouroboros_probe",
            skill_path="skills/second/SKILL.md",
            mcp_args={"cwd": "$CWD", "artifact": "$artifact"},
            context_keys=("cwd", "artifact"),
        ),
    )

    assert skill_frontmatter_context_keys_by_tool(mappings) == {
        "ouroboros_probe": ("session_id", "cwd", "artifact")
    }


def test_merge_tool_context_keys_prefers_frontmatter_order_and_normalizes() -> None:
    assert merge_tool_context_keys(
        (" session_id ", "cwd", "", "artifact"),
        ("cwd", " cursor ", "artifact", "  "),
    ) == ("session_id", "cwd", "artifact", "cursor")


def test_merge_skill_context_keys_by_tool_keeps_body_only_tools() -> None:
    assert merge_skill_context_keys_by_tool(
        {
            "ouroboros_execute_seed": ("seed_path", "cwd"),
            "ouroboros_session_status": ("session_id",),
        },
        {
            "ouroboros_execute_seed": ("seed_content", "session_id"),
            "ouroboros_job_wait": ("job_id", "cursor"),
        },
    ) == {
        "ouroboros_execute_seed": (
            "seed_path",
            "cwd",
            "seed_content",
            "session_id",
        ),
        "ouroboros_session_status": ("session_id",),
        "ouroboros_job_wait": ("job_id", "cursor"),
    }


def test_discover_skill_context_keys_merges_packaged_frontmatter_and_body_usage() -> None:
    context_keys_by_tool = discover_skill_context_keys()

    assert context_keys_by_tool["ouroboros_execute_seed"] == (
        "seed_path",
        "cwd",
    )
    assert context_keys_by_tool["ouroboros_start_execute_seed"] == (
        "seed_content",
        "session_id",
    )
    assert context_keys_by_tool["ouroboros_start_auto"] == (
        "goal",
        "resume",
        "cwd",
        "max_interview_rounds",
        "max_repair_rounds",
        "skip_run",
        "complete_product",
        "pipeline_timeout_seconds",
    )
    assert context_keys_by_tool["ouroboros_interview"] == (
        "initial_context",
        "cwd",
        "session_id",
        "answer",
        "last_question",
    )
    assert context_keys_by_tool["ouroboros_job_wait"] == ("job_id", "cursor")
