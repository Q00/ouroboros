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

    assert "question_advisory_subagents` is present you MUST process every" in skill_text
    assert 'dispatch_mode="host_decides"' in skill_text
    assert "Task/Agent" in skill_text
    assert "one native" in skill_text
    assert "dispatch_subagents_if_supported" in skill_text
    assert "process_payloads_sequentially" in skill_text
    assert "host action selects the execution strategy" in skill_text
    assert "Never reconstruct" in skill_text and "prompts from prose" in skill_text
    assert "`run_lateral_review`" in skill_text
    assert "**Milestone lateral-review dispatch**" in skill_text
    assert "meta.lateral_review_tool_args" in skill_text
    assert "required lightweight subagent review" in skill_text
    assert "Main-session direct-answer assistance" in skill_text


def test_claude_plugin_unstuck_skill_includes_host_capability_contract() -> None:
    skill_path = Path(".claude-plugin") / "skills" / "unstuck" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")

    assert (
        '{"dispatch_mode": "host_decides", "host_action": "dispatch_subagents_if_supported"'
    ) in skill_text
    assert '"execution_preference": "parallel"' in skill_text
    assert '"fallback_strategy": "sequential"' in skill_text
    assert "capability-neutral `host_decides`" in skill_text
    assert "##### Debate, constrained runtime without sub-agent dispatch" in skill_text
    assert 'dispatch_mode="sequential"' in skill_text
    assert 'dispatch_mode="host_decides"' in skill_text
    assert "result_correlation_key" in skill_text
    assert 'legacy_dispatch_mode="inline_fallback"' in skill_text
    assert 'Debate response (`dispatch_mode = "inline_fallback"`)' not in skill_text
