"""Tests for Claude plugin skill execution guide artifact."""

from pathlib import Path

from ouroboros.backends.capabilities import render_backend_skill_capability_guide


def test_claude_plugin_ships_rendered_skill_capability_guide() -> None:
    guide_path = Path(".claude-plugin") / "SKILL_CAPABILITY_GUIDE.md"

    # The Claude plugin artifact is generated from the backend capability registry;
    # update it by rendering this helper rather than hand-editing the snapshot.
    assert guide_path.read_text(encoding="utf-8") == render_backend_skill_capability_guide("claude")


def test_claude_plugin_interview_skill_includes_lateral_review_dispatch() -> None:
    skill_path = Path("skills") / "interview" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")

    assert "question_advisory_subagents` is present you MUST process every" in skill_text
    assert 'dispatch_mode="host_decides"' in skill_text
    # Parallel fan-out is named by capability; each runtime's guide maps it.
    assert "one child per payload through the active runtime's `orchestrate_subagents`" in (
        skill_text
    )
    assert "single fan-out turn" in skill_text
    assert "Task/Agent" not in skill_text
    assert "native subagent" not in skill_text
    assert "Task/Agent" in render_backend_skill_capability_guide("claude")
    assert "one Codex subagent per payload" in render_backend_skill_capability_guide("codex")
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
    skill_path = Path("skills") / "unstuck" / "SKILL.md"
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


def test_interview_skill_pins_seed_ready_guard_lane_output_shapes() -> None:
    skill_text = (Path("skills") / "interview" / "SKILL.md").read_text(encoding="utf-8")
    step8 = skill_text.split("8. **Seed-ready Acceptance Guard.**", 1)[1].split(
        "9. **Restate gate**", 1
    )[0]

    assert (
        '{"lane_id": "closer", "verdict": "seed_ready" | "not_ready", "reason": "...", '
        '"blocking_question": "..." | null}'
    ) in step8
    assert (
        '{"lane_id": "<lane>", "severity": "high" | "medium" | "low", "finding": "...", '
        '"question": "..." | null}'
    ) in step8
