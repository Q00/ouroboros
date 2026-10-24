"""Contract tests for the topic-specific interview calibration skill."""

from __future__ import annotations

from pathlib import Path

from ouroboros.router import Resolved, ResolveRequest, resolve_skill_dispatch

IDK_SKILL = Path("skills/idk/SKILL.md")
INTERVIEW_SKILL = Path("skills/interview/SKILL.md")
DEV_AGENTS = Path("AGENTS.md")


def test_idk_skill_defines_topic_specific_calibration_contract() -> None:
    text = IDK_SKILL.read_text(encoding="utf-8")

    assert "name: idk" in text
    assert "Foundational" in text
    assert "Working" in text
    assert "Fluent" in text
    assert "confidence" in text
    assert "topic-specific interview calibration" in text
    assert "Do not write a profile to disk" in text


def test_idk_routes_through_the_interview_session_contract(tmp_path: Path) -> None:
    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo idk I do not know idempotency; I built REST APIs",
            cwd=tmp_path,
            skills_dir=Path("skills"),
        )
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "idk"
    assert result.mcp_tool == "ouroboros_interview"
    assert result.mcp_args == {"calibration_input": "I do not know idempotency; I built REST APIs"}


def test_idk_does_not_consume_a_pending_interview_question() -> None:
    text = IDK_SKILL.read_text(encoding="utf-8")

    assert "Do not treat the calibration text as the answer" in text
    assert "Ask the rephrased question again" in text


def test_interview_applies_idk_calibration_without_reducing_rigor() -> None:
    text = INTERVIEW_SKILL.read_text(encoding="utf-8")

    assert "most recent `Interview calibration` produced by `ooo idk`" in text
    assert "preserve the original question's decision" in text
    assert "do not forward that statement to MCP as the answer" in text


def test_dev_mode_agents_routes_and_summarizes_idk() -> None:
    text = DEV_AGENTS.read_text(encoding="utf-8")

    assert "`ooo idk ...` | Read `skills/idk/SKILL.md`" in text
    assert "`ooo idk` | MCP: `ouroboros_interview` calibration control turn" in text


def test_claude_plugin_idk_skill_exists() -> None:
    test_file = Path(__file__).resolve()
    repo_root = test_file.parent
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    skill_path = repo_root / "skills" / "idk" / "SKILL.md"
    assert skill_path.exists(), f"Missing: {skill_path}"
    content = skill_path.read_text()
    assert "mcp_tool: ouroboros_interview" in content
    assert "calibration_input" in content


def test_claude_plugin_idk_skill_instructs_calibration_relay() -> None:
    test_file = Path(__file__).resolve()
    repo_root = test_file.parent
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    skill_path = repo_root / "skills" / "idk" / "SKILL.md"
    content = skill_path.read_text()
    assert "meta.interview_calibration" in content
    assert "interview_calibration" in content
    assert "subsequent" in content.lower()


def test_claude_plugin_interview_skill_instructs_calibration_relay() -> None:
    test_file = Path(__file__).resolve()
    repo_root = test_file.parent
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    skill_path = repo_root / "skills" / "interview" / "SKILL.md"
    content = skill_path.read_text()
    assert "interview_calibration" in content
    assert "interview_calibration" in content and "argument" in content.lower()
