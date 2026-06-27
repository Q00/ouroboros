"""Tests for Claude plugin skill execution guide artifact."""

from pathlib import Path

from ouroboros.backends.capabilities import render_backend_skill_capability_guide


def test_claude_plugin_ships_rendered_skill_capability_guide() -> None:
    guide_path = Path(".claude-plugin") / "SKILL_CAPABILITY_GUIDE.md"

    # The Claude plugin artifact is generated from the backend capability registry;
    # update it by rendering this helper rather than hand-editing the snapshot.
    assert guide_path.read_text(encoding="utf-8") == render_backend_skill_capability_guide("claude")


def test_claude_plugin_interview_skill_includes_lateral_review_dispatch() -> None:
    skill_path = Path(".claude-plugin") / "skills" / "interview" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")

    assert "question_advisory_subagents` is present you MUST fan out" in skill_text
    assert "Task/Agent" in skill_text
    assert "a reinforcing cue for host-driven runtimes" in skill_text
    assert "as a prerequisite" in skill_text
    assert "`run_lateral_review`" in skill_text
    assert "**Milestone lateral-review dispatch**" in skill_text
    assert "meta.lateral_review_tool_args" in skill_text
    assert "required lightweight subagent review" in skill_text
    assert "Main-session direct-answer assistance" in skill_text
